"""Single source of truth for the embedding function.

Used by:
  - MCP server (dedupe check during write_note)
  - db.py (upsert + query embedding)
  - federation adapters that need to embed queries (e.g. mycelium-notes)

Direct onnxruntime + the bundled all-MiniLM-L6-v2 ONNX model (vendored under
``mycelium/_models/``). This replaces the heavy ``chromadb`` dependency, which was
previously pulled in ONLY for its bundled ONNX embedder. The model file and forward
pass are byte-for-byte the same as chromadb's ``ONNXMiniLM_L6_V2`` (same model.onnx,
same tokenizer.json, same fixed 256-token padding, attention-masked mean pooling and
L2 normalisation), so embeddings remain identical to the existing pgvector index —
no reindex required. See tests/test_embedder_parity.py for the parity gate.

The model loads read-only from the installed package, so it works under a
read-only root filesystem with no runtime download and no writable cache.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np

# Vendored model (copied verbatim from chroma's all-MiniLM-L6-v2 onnx bundle).
_MODEL_DIR = Path(__file__).resolve().parent / "_models" / "all-MiniLM-L6-v2"
_MAX_TOKENS = 256  # sentence-transformers uses 256 for this model (not the HF 128)


@lru_cache(maxsize=1)
def _runtime():
    """Lazily build the (onnxruntime session, tokenizer). Cached singleton."""
    import onnxruntime as ort
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(_MODEL_DIR / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=_MAX_TOKENS)
    tokenizer.enable_padding(pad_id=0, pad_token="[PAD]", length=_MAX_TOKENS)

    so = ort.SessionOptions()
    so.log_severity_level = 3
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    providers = list(ort.get_available_providers())
    # CoreML is less optimised than CPU for this model (matches chroma's choice).
    if "CoreMLExecutionProvider" in providers:
        providers.remove("CoreMLExecutionProvider")

    session = ort.InferenceSession(
        str(_MODEL_DIR / "model.onnx"), sess_options=so, providers=providers
    )
    return session, tokenizer


def _normalize(v: np.ndarray) -> np.ndarray:
    """L2-normalise rows; pytorch's epsilon for div-by-zero (matches chroma)."""
    norm = np.linalg.norm(v, axis=1)
    norm[norm == 0] = 1e-12
    return (v / norm[:, np.newaxis]).astype(np.float32)


def embed_batch(texts: list[str]) -> np.ndarray:
    """Embed a list of texts → (n, 384) float32 array. Identical recipe to
    chroma's ONNXMiniLM_L6_V2._forward (fixed 256 padding, zero token_type_ids,
    attention-masked mean pooling, L2 normalise)."""
    session, tokenizer = _runtime()
    encoded = [tokenizer.encode(t) for t in texts]
    input_ids = np.array([e.ids for e in encoded], dtype=np.int64)
    attention_mask = np.array([e.attention_mask for e in encoded], dtype=np.int64)
    token_type_ids = np.zeros_like(input_ids, dtype=np.int64)

    last_hidden_state = session.run(
        None,
        {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "token_type_ids": token_type_ids,
        },
    )[0]

    mask = np.broadcast_to(
        np.expand_dims(attention_mask, -1), last_hidden_state.shape
    )
    pooled = np.sum(last_hidden_state * mask, axis=1) / np.clip(
        mask.sum(axis=1), a_min=1e-9, a_max=None
    )
    return _normalize(pooled)


def embed(text: str) -> list[float]:
    """Embed a single text. Returns a 384-dim list of floats."""
    return embed_batch([text])[0].tolist()
