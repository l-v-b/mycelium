"""Compatibility shim — storage moved from ChromaDB to pgvector.

The backend is now PostgreSQL + pgvector (see mycelium/db.py). This module is
retained ONLY so existing imports (`from mycelium.chroma import
notes_collection`, etc.) keep working without touching every call site. The
returned objects are pgvector-backed Collection shims exposing the same
ChromaDB method surface (.count/.delete/.query/.upsert/.get).

New code should import from mycelium.db directly. This shim is a thin
re-export and can be removed once call sites are migrated.
"""
from __future__ import annotations

from mycelium.db import (  # noqa: F401
    closets_collection,
    drawers_collection,
    get_client,
    links_collection,
    notes_collection,
)
