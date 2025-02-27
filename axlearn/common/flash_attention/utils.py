# Copyright © 2023 Apple Inc.

"""FlashAttention utilities shared amongst CPU/GPU/TPU backends."""
import functools
from typing import Callable, Literal, Optional

import jax
import jax.numpy as jnp
from absl import logging

from axlearn.common.attention import softmax_with_biases
from axlearn.common.attention_bias import (
    NEG_INF,
    BaseAttentionBias,
    CausalAttentionBias,
    CompositeAttentionBias,
    MaskFnAttentionBias,
    SegmentIdAttentionBias,
    TensorAttentionBias,
    ZeroAttentionBias,
    split,
)
from axlearn.common.flash_attention.gpu_attention import cudnn_dot_product_attention
from axlearn.common.flash_attention.gpu_attention import flash_attention as gpu_flash_attention
from axlearn.common.flash_attention.gpu_decoding import flash_decoding
from axlearn.common.flash_attention.tpu_attention import tpu_flash_attention
from axlearn.common.utils import Tensor


@functools.partial(jax.jit, static_argnames=["causal", "softmax_scale"])
@jax.default_matmul_precision("bfloat16")
def mha_reference(
    q: Tensor,
    k: Tensor,
    v: Tensor,
    bias: Optional[Tensor] = None,
    segment_ids: Optional[Tensor] = None,
    *,
    causal: bool = False,
    softmax_scale: float = 1.0,
) -> Tensor:
    """Reference multi-headed attention implementation.

    Args:
        q: query tensor with shape [batch_size, seq_len, num_heads, per_head_dim]
        k: key tensor with shape [batch_size, seq_len, num_heads, per_head_dim]
        v: value tensor with shape [batch_size, seq_len, num_heads, per_head_dim]
        bias: bias tensor with a shape that can broadcast to
            [batch_size, num_heads, seq_len, seq_len], e.g. [1, 1, seq_len, seq_len].
        segment_ids: segment ids tensor with shape [batch_size, seq_len].
        causal: whether the attention is causal.
        softmax_scale: a scalar value applied to the logits before softmax.

    Returns:
        A tensor with shape [batch_size, seq_len, num_heads, per_head_dim].
    """
    # We apply the scale factor before the attention biases.
    q *= softmax_scale
    logits = jnp.einsum("btnh,bsnh->bnts", q, k)

    # Check if we need to build a segment id mask.
    if segment_ids is not None:
        assert segment_ids.ndim == 2  # shape [batch_size, seq_len]
        target_segment_ids = jnp.expand_dims(segment_ids, -1)
        source_segment_ids = jnp.expand_dims(segment_ids, -2)
        # Target [b..., t] + Source [b..., s] -> [b..., t, s]
        # [b, 1, ..., t, s] where the value at [..., i, j] = false if
        # target_segments[..., i] == source_segments[..., j], or true otherwise.
        mask = jax.lax.ne(source_segment_ids, target_segment_ids)[:, None, ...]
        logits = jnp.where(mask, NEG_INF, logits)

    if causal:
        mask_shape = (q.shape[1], k.shape[1])
        row_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 0)
        col_ids = jax.lax.broadcasted_iota(jnp.int32, mask_shape, 1)
        mask = (row_ids < col_ids)[None, None, :, :]  # Causal mask.
        logits = jnp.where(mask, NEG_INF, logits)

    probs = softmax_with_biases(logits, bias)
    context = jnp.einsum("bnts,bsnh->btnh", probs, v).astype(v.dtype)
    return context


def _repeat_kv_heads(num_q_heads: int, key_or_value: Tensor) -> Tensor:
    """Repeats key or value heads dim to match the query.

    TODO(dhwang2): optimize computation like GroupedQueryAttention.
    """
    num_head_repeats = num_q_heads // key_or_value.shape[-2]
    if num_head_repeats == 1:
        return key_or_value
    # Repeat along the num_heads dim: [batch, source_length, num_heads, per_head_dim].
    return jnp.repeat(key_or_value, num_head_repeats, axis=-2)


# Accepts [query, key, value, attention_bias, segment_ids] tensors and returns the context Tensor.
MultiHeadAttentionImpl = Callable[[Tensor, Tensor, Tensor, Tensor, Tensor], Tensor]


def flash_attention_implementation(
    backend: Literal["cpu", "tpu", "gpu", "xla"],
    *,
    softmax_scale: float,
    block_size: int = 128,
) -> MultiHeadAttentionImpl:
    """Returns a jitted "flash" multihead-attention implementation for the given backend.

    Args:
        backend: A valid XLA backend name. 'cpu' intended for testing only.
        softmax_scale: A scalar value applied to the logits before softmax.
        block_size: The size of the computation-block unit, only applies to the 'tpu' backend.
            A multiple of 128, and should be less than the target sequence length.
            Smaller values are more memory efficient but less compute efficient.

    Returns:
        A jitted function implementing multi-head attention for the given backend.

    Raises:
        NotImplementedError: If implementation for the backend is not available.
    """

    # shard_map-decorated function needs to be jitted.
    @jax.jit
    def jit_attn(
        query: Tensor,
        key: Tensor,
        value: Tensor,
        bias: BaseAttentionBias,
        *,
        backend: str = backend,
    ) -> Tensor:
        # Fall back to plain MHA implementation when the seq_len is not be divisible by
        # block size.
        is_gpu_decoding = query.shape[1] == 1 and backend == "gpu"
        if not is_gpu_decoding and query.shape[1] % block_size != 0:
            backend = "xla"
        # For non-GPU decoding, fall back to non-flash implementation and merge all biases
        # into a dense floating point bias tensor since that implementation does not
        # support target_positions.
        if not is_gpu_decoding and query.shape[1] == 1:
            # TODO(senyut): Support TPU decoding.
            backend = "xla"
            bias = TensorAttentionBias(bias.value())

        bias = CompositeAttentionBias([bias])

        def get_segment_ids(segment_ids: SegmentIdAttentionBias) -> Optional[Tensor]:
            """Return the segment ids Tensor from the sequence of segment ids attention
            biases or None if there are no segment ids.
            """
            if segment_ids is None or segment_ids.value() is None:
                return None
            if query.shape[1] != key.shape[1]:
                raise ValueError(
                    "segment_ids is only supported for query and key with identical lengths."
                )
            if segment_ids.eval_shape()[0] != query.shape[0]:
                raise ValueError(
                    "segment_ids must have matching batch dim: "
                    f"{segment_ids.eval_shape()} vs. {query.shape[0]}"
                )
            return segment_ids.segment_ids

        if backend == "gpu":
            # TODO(hanzhi-zhou): supports small q sequence length for future use cases such as
            # speculative decoding.
            if query.shape[1] == 1:
                # Decoding case. We should not repeat kv heads to match q heads for FlashDecoding.
                # Note: decoding is always causal. Discard the causal mask if present.
                mask, explicit_bias = split(bias, MaskFnAttentionBias)
                if mask is None or mask.target_positions is None:
                    raise RuntimeError("Cannot retrive MaskFnAttentionBias or target_positions.")
                mask_fn = mask.mask
                kv_seq_len = mask.target_positions + 1
                logging.info("Using mask_fn=%s for FlashDecoding.", mask_fn)

                bias = explicit_bias.value()
                if bias is not None:
                    logging.info(
                        "Using explicit_bias=%s for FlashDecoding. "
                        "This is not expected unless an explicit Tensor bias is used.",
                        bias,
                    )
                return flash_decoding(
                    query,
                    key,
                    value,
                    bias=bias,
                    mask_fn=mask_fn,
                    kv_seq_len=kv_seq_len,
                    softmax_scale=softmax_scale,
                )

            if query.shape[1] != key.shape[1]:
                # TODO(xuan-zou): Generalize GPU Flash Attention for q_len != kv_len.
                # Remove pytest.skip corresponding to q_len != kv_len in layer_test.py once fixed.
                raise NotImplementedError(
                    f"Query length {query.shape[1]} must be equal to KV length "
                    f"{key.shape[1]} for correctly supported GPU flash attention usage."
                )

            key = _repeat_kv_heads(query.shape[2], key)
            value = _repeat_kv_heads(query.shape[2], value)

            # We have two implementations to choose from.
            # Both support `causal`.
            # One supports `segment_ids`.
            causal, segment_ids, explicit_bias = split(
                bias, CausalAttentionBias, SegmentIdAttentionBias
            )

            # Fall back to triton gpu kernel if:
            # - segment_ids is not None, or
            # - explicit_bias is not empty, or
            # - query/key/value is in float32.
            if (
                segment_ids.value() is not None
                or explicit_bias.value() is not None
                or jnp.float32 in (query.dtype, key.dtype, value.dtype)
            ):
                logging.warning("Flash attention falling back to Triton GPU kernel.")
                return gpu_flash_attention(
                    query,
                    key,
                    value,
                    bias=explicit_bias.value(),
                    segment_ids=get_segment_ids(segment_ids),
                    softmax_scale=softmax_scale,
                    causal=causal.value() is not None,
                )
            else:
                explicit_bias += segment_ids
                return cudnn_dot_product_attention(
                    query,
                    key,
                    value,
                    bias=explicit_bias.value(),
                    softmax_scale=softmax_scale,
                    causal=causal.value() is not None,
                    dropout_rate=0.0,
                )

        elif backend == "tpu":
            # TODO(dhwang2): splash attention supports GQA natively, so don't repeat it.
            # https://github.com/jax-ml/jax/blob/7b9914d711593dca8725d46aa1dadb2194284519/jax/experimental/pallas/ops/tpu/splash_attention/splash_attention_kernel.py#L934
            key = _repeat_kv_heads(query.shape[2], key)
            value = _repeat_kv_heads(query.shape[2], value)
            # `mask` is supported.
            # `segment_ids` is supported.
            # Optimized handling for the above two types.
            # Fallback for types that aren't instances of either of the above.
            mask, segment_ids, explicit_bias = split(
                bias, MaskFnAttentionBias, SegmentIdAttentionBias
            )
            return tpu_flash_attention(
                query,
                key,
                value,
                bias=explicit_bias.value(),
                segment_ids=get_segment_ids(segment_ids),
                # The `from_sequence()` function guarantees that if there is only one
                # mask, it is returned without modification.
                # This allows the `causal` path in `_legacy_tpu_flash_attention()` to work.
                mask=mask if not isinstance(mask, ZeroAttentionBias) else None,
                softmax_scale=softmax_scale,
                block_size=block_size,
            )

        elif backend in ("cpu", "xla"):
            key = _repeat_kv_heads(query.shape[2], key)
            value = _repeat_kv_heads(query.shape[2], value)
            if backend == "cpu":
                logging.warning("Flash attention CPU backend is for testing only.")
            logging.warning("Flash attention falling back using plain MHA implementation")

            # `causal` is supported.
            # `segment_ids` is supported.
            causal, segment_ids, explicit_bias = split(
                bias, CausalAttentionBias, SegmentIdAttentionBias
            )
            return mha_reference(
                query,
                key,
                value,
                bias=explicit_bias.value(),
                segment_ids=get_segment_ids(segment_ids),
                causal=causal.value() is not None,
                softmax_scale=softmax_scale,
            )

        raise NotImplementedError(f"Backend ({backend}) does not have an implementation.")

    return jit_attn
