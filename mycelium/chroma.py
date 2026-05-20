"""ChromaDB client and collection initialisation.

Four collections:
  mycelium_notes    — curated synthesis notes (document = full note content)
  mycelium_drawers  — verbatim captures (document = raw text content)
  mycelium_links    — typed semantic links (document = natural-language description)
  mycelium_closets  — topical pointers boosting drawer ranking (document = topic
                      summary with embedded drawer_id refs like "→drawer_xxx,drawer_yyy")

All use all-MiniLM-L6-v2 via ChromaDB's default embedding function.
ChromaDB is a derived search index — the vault/ markdown files are canonical.
Closets are an exception: they are LLM/regex-extracted from drawers and live
only in ChromaDB. Treat them as a rebuildable cache, not source of truth.
"""
from __future__ import annotations

import chromadb
from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

from mycelium.config import CHROMA_DIR

_client: chromadb.PersistentClient | None = None
_ef = DefaultEmbeddingFunction()


def get_client() -> chromadb.PersistentClient:
    global _client
    if _client is None:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _client


def notes_collection() -> chromadb.Collection:
    return get_client().get_or_create_collection(
        name="mycelium_notes",
        embedding_function=_ef,
        metadata={"hnsw:space": "cosine"},
    )


def drawers_collection() -> chromadb.Collection:
    return get_client().get_or_create_collection(
        name="mycelium_drawers",
        embedding_function=_ef,
        metadata={"hnsw:space": "cosine"},
    )


def links_collection() -> chromadb.Collection:
    return get_client().get_or_create_collection(
        name="mycelium_links",
        embedding_function=_ef,
        metadata={"hnsw:space": "cosine"},
    )


def closets_collection() -> chromadb.Collection:
    return get_client().get_or_create_collection(
        name="mycelium_closets",
        embedding_function=_ef,
        metadata={"hnsw:space": "cosine"},
    )
