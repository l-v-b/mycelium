"""Frontmatter helpers for committed_at, author, source_intent.

These are the per-write metadata fields stamped on every disk write in v1.0.5+.
Existing files without these fields are treated as valid (load helpers default
the missing keys gracefully) — no migration needed for the personal stack.

Field semantics:

- committed_at:   ISO-8601 UTC timestamp of the chroma commit. None means
                  "pending" (team-mode draft awaiting writer worker).
                  In personal mode every write stamps a real timestamp
                  immediately because the commit happens synchronously.
- author:         Stable identifier of the agent / human who wrote this entry.
                  Falls back to MYCELIUM_AUTHOR env / git config / "unknown".
                  Future team mode will derive from the auth context
                  (EntraID claim via ContextForge).
- source_intent:  Required for write_note (synthesis) when v2.0.0 lands;
                  optional today. Captures the agent's reasoning about why
                  the note was written — useful provenance for future search.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def stamp_committed(extras: dict, committed: bool = True) -> dict:
    """Stamp `committed_at` on a frontmatter dict.

    Pass `committed=False` to mark the file as pending (team-mode draft).
    The writer worker will flip it to a real timestamp once chroma upsert
    succeeds.
    """
    extras["committed_at"] = now_iso() if committed else None
    return extras


def stamp_author(extras: dict, author: str | None = None) -> dict:
    """Stamp `author` on a frontmatter dict.

    Falls back to mycelium.config.AUTHOR (env / git config / "unknown").
    Future team mode will pass an explicit author derived from the
    auth context; in personal mode the fallback is always used.
    """
    if author is None:
        from mycelium.config import AUTHOR
        author = AUTHOR
    extras["author"] = author
    return extras


def stamp_intent(extras: dict, intent: str | None) -> dict:
    """Stamp `source_intent` on a frontmatter dict.

    For v1.x: optional — None is allowed (key simply not set).
    For v2.0.0: write_note will enforce a non-empty intent before reaching here.
    """
    if intent is not None and intent.strip():
        extras["source_intent"] = intent.strip()
    return extras
