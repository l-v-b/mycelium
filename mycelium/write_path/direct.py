"""Personal-mode (synchronous) write path.

This is the today's behaviour: write to disk + upsert to ChromaDB inside the
same MCP call, return when both are done. No Redis, no worker, no async lag.

Team mode (queued.py) uses the same disk-write step but defers the ChromaDB
upsert to an out-of-process worker.
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

from mycelium import metrics, vault


def write_note_direct(
    title: str,
    content: str,
    tags: list[str] | None,
    source_memories: list[str] | None,
    status: str | None,
    intent: str | None,
    query_notes_fn: Callable[[str, int], str],
    upsert_note_fn: Callable[..., None],
    load_note_fn: Callable[..., dict | None],
) -> str:
    """Personal-mode write_note. Returns the same response string as the v1.0.x
    MCP tool surface ("Note written: <id>\\nFile: <path>[\\nWarning: ...]").

    Dependencies are injected (query/upsert/load functions) to avoid importing
    server.py from here — server.py imports this module.
    """
    from mycelium.write_path.dedupe import check_duplicate_note

    started = time.perf_counter()
    tags = tags or []
    source_memories = source_memories or []

    duplicate_warning = check_duplicate_note(title, query_notes_fn)
    if duplicate_warning:
        metrics.incr("dedupe_warning_total")

    nid, filepath = vault.write_note(
        title, content, tags, source_memories, status,
        intent=intent,  # may be None today; required in v2.0.0
        committed=True,
    )

    loaded = load_note_fn(filepath)
    final_status = loaded.get("status", "") if loaded else (status or "")

    # Build chroma metadata. Same shape as v1.0.x to preserve search behaviour.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    chroma_metadata: dict = {
        "title":           title,
        "tags":            json.dumps(tags),
        "source_memories": json.dumps(source_memories),
        "created_at":      now,
        "filepath":        str(filepath),
    }
    if final_status:
        chroma_metadata["status"] = final_status

    upsert_note_fn(nid, title, content, chroma_metadata)

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.observe("write_latency_ms", elapsed_ms, {"tool": "write_note", "mode": "personal"})
    metrics.incr("write_total", {"tool": "write_note", "mode": "personal", "outcome": "committed"})

    confirmation = f"Note written: {nid}\nFile: {filepath}"
    if duplicate_warning:
        confirmation += f"\nWarning: {json.dumps(duplicate_warning, indent=2)}"
    return confirmation


def file_direct(
    content: str,
    wing: str,
    room: str,
    upsert_drawer_fn: Callable[..., None],
) -> str:
    """Personal-mode file (verbatim drawer). No dedupe (express lane)."""
    started = time.perf_counter()
    did, filepath = vault.write_drawer(content, wing, room, committed=True)
    upsert_drawer_fn(did, content, wing, room, filepath)

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.observe("write_latency_ms", elapsed_ms, {"tool": "file", "mode": "personal"})
    metrics.incr("write_total", {"tool": "file", "mode": "personal", "outcome": "committed"})

    return f"Filed: {did} → {wing}/{room}"


def diary_write_direct(
    content: str,
    session_id: str,
    upsert_diary_fn: Callable[..., None] | None = None,
) -> str:
    """Personal-mode diary_write. The existing vault.diary_write already
    handles indexing internally, so the upsert callback is currently unused."""
    started = time.perf_counter()
    filepath = vault.diary_write(content, session_id)

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.observe("write_latency_ms", elapsed_ms, {"tool": "diary_write", "mode": "personal"})
    metrics.incr("write_total", {"tool": "diary_write", "mode": "personal", "outcome": "committed"})

    return f"Diary entry written: {filepath}"
