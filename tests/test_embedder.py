"""Embedder regression guard for the all-MiniLM-L6-v2 ONNX model.

Golden values were captured at the chromadb->onnxruntime migration (v2.4.0),
where the direct-onnxruntime embedder was verified BIT-IDENTICAL to chromadb's
ONNXMiniLM_L6_V2 (max abs diff 0.0). A drift here means the vendored model.onnx,
the tokenizer, or the forward pass changed — which would desync the pgvector
index. This test needs no chromadb (which we dropped); it locks behaviour
against the captured golden vector.
"""
from __future__ import annotations

import numpy as np

from mycelium.embedder import embed, embed_batch

_TEXT = "mycelium embedder parity sentinel"
_GOLD_SUM = 0.19714177
_GOLD_FIRST8 = [
    0.00963687, -0.10557694, 0.00865447, -0.03567915,
    0.00979853, -0.03946881, -0.05236815, 0.07097864,
]


def test_dim_and_unit_norm():
    v = np.asarray(embed(_TEXT), dtype=np.float32)
    assert v.shape == (384,)
    assert abs(float(np.linalg.norm(v)) - 1.0) < 1e-4


def test_golden_values():
    v = np.asarray(embed(_TEXT), dtype=np.float32)
    assert abs(float(v.sum()) - _GOLD_SUM) < 1e-4
    assert np.allclose(v[:8], _GOLD_FIRST8, atol=1e-4)


def test_batch_matches_single():
    batched = np.asarray(embed_batch([_TEXT, "another string"]), dtype=np.float32)
    single = np.asarray([embed(_TEXT), embed("another string")], dtype=np.float32)
    assert np.allclose(batched, single, atol=1e-6)
