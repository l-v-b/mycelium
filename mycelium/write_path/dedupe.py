"""Deterministic duplicate detection for write_note. No LLM.

Extracted from the inline check in server.py:478 (v1.0.4 and earlier).
Same threshold (0.5 — MiniLM cosine distances for this corpus). Same
semantics: return a warning when a near-duplicate exists with a DIFFERENT
title; same-title cases are upserts and don't warrant a warning.

The check is best-effort: any failure (chroma down, malformed metadata,
etc.) returns None and the write proceeds without warning. Never blocks.
"""
from __future__ import annotations

import json
from typing import Callable, Optional


# Threshold reflects MiniLM cosine distances for this corpus:
# near-identical titles register ~0.4–0.5; tangentially related stuff
# sits at 0.6+. 0.5 catches the obvious dups without false positives.
DUPLICATE_DISTANCE_THRESHOLD = 0.5


def check_duplicate_note(
    title: str,
    query_notes_fn: Callable[[str, int], str],
) -> Optional[dict]:
    """Return a duplicate-warning dict if a near-duplicate note exists.

    Args:
        title: Proposed note title.
        query_notes_fn: callable (query, n_results) -> JSON string from the
            mycelium-query-notes tool. Injected to avoid a circular import
            (server.py imports this module, and the function lives in server.py).

    Returns:
        {message, existing_title, existing_note_id, distance} when:
          - top-1 vector match has distance < DUPLICATE_DISTANCE_THRESHOLD
          - existing title differs from proposed (same title is an upsert)
        Otherwise None.

    Never raises. Any internal failure returns None.
    """
    try:
        existing_query = json.loads(query_notes_fn(title, 1))
        candidates = existing_query.get("notes", [])
        if not candidates:
            return None
        top = candidates[0]
        top_title = (top.get("title") or "")
        top_distance = top.get("distance", 1.0)
        if (
            top_distance < DUPLICATE_DISTANCE_THRESHOLD
            and top_title.strip().lower() != title.strip().lower()
        ):
            return {
                "message":          "Similar existing note found — consider updating that one instead (write_note upserts by title).",
                "existing_title":   top_title,
                "existing_note_id": top.get("note_id"),
                "distance":         top_distance,
            }
    except Exception:
        # Never let the lookup block a real write.
        pass
    return None
