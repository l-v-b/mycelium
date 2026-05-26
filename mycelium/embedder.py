"""Single source of truth for the embedding function.

Used by:
  - MCP server (dedupe check during write_note)
  - writer worker, when team mode lands (re-check at commit)
  - federation adapters that need to embed queries (e.g. mycelium-notes)

Both server and worker import THIS function; both use the bundled ONNX model
from chromadb's default embedder. Embeddings are guaranteed consistent because
both processes load the same model from the same package install.

In personal mode there's only one process — consistency is trivial. The
module exists so PR-B's writer worker has a non-collection-coupled entrypoint
to call.
"""
from __future__ import annotations

_embedder = None


def get_embedder():
    """Return the singleton ChromaDB ONNX embedder."""
    global _embedder
    if _embedder is None:
        # Import lazily so importing this module doesn't pay the ONNX startup cost.
        from chromadb.utils import embedding_functions
        _embedder = embedding_functions.ONNXMiniLM_L6_V2()
    return _embedder


def embed(text: str) -> list[float]:
    """Embed a single text. Returns a 384-dim float vector."""
    return get_embedder()([text])[0]
