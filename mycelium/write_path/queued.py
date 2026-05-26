"""Team-mode (async via Redis Streams) write path.

Producer side: writes markdown to disk with committed_at: null, XADDs a
lightweight signal to the Redis stream, returns immediately. The writer
worker (worker.py) reads the stream and performs the ChromaDB upsert
asynchronously, flipping committed_at to a real timestamp on success.

If Redis is unavailable, this path fails the MCP call rather than silently
falling back to direct writes — operators need to see the breakage.
"""
from __future__ import annotations

import json
import time
from typing import Callable

from mycelium import metrics, vault
from mycelium.write_path.dedupe import check_duplicate_note

STREAM_KEY = "mycelium:writes"


def _redis():
    """Lazily import + connect. Raises ImportError with a clear message if
    redis-py isn't installed (personal users don't pay this import cost)."""
    try:
        import redis
    except ImportError as e:
        raise RuntimeError(
            "MYCELIUM_DEPLOYMENT_MODE=team requires redis-py. "
            "Install with: uv pip install mycelium-palace[team]"
        ) from e
    from mycelium import config
    return redis.Redis.from_url(getattr(config, "REDIS_URL", "redis://localhost:6379/0"))


def write_note_queued(
    title: str,
    content: str,
    tags: list[str] | None,
    source_memories: list[str] | None,
    status: str | None,
    intent: str | None,
    query_notes_fn: Callable[[str, int], str],
) -> str:
    """Team-mode write_note. Returns confirmation with pending_id; worker
    commits to chroma asynchronously (~minutes lag worst-case)."""
    started = time.perf_counter()
    tags = tags or []
    source_memories = source_memories or []

    duplicate_warning = check_duplicate_note(title, query_notes_fn)
    if duplicate_warning:
        metrics.incr("dedupe_warning_total")

    nid, filepath = vault.write_note(
        title, content, tags, source_memories, status,
        intent=intent,
        committed=False,   # writer worker flips to a real timestamp
    )

    r = _redis()
    r.xadd(STREAM_KEY, {"kind": "note", "id": nid, "action": "upsert"})

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.observe("write_latency_ms", elapsed_ms, {"tool": "write_note", "mode": "team"})
    metrics.incr("write_total", {"tool": "write_note", "mode": "team", "outcome": "enqueued"})

    confirmation = (
        f"Note enqueued: {nid}\nFile: {filepath}\n"
        f"(team mode: writer worker will commit to chroma asynchronously)"
    )
    if duplicate_warning:
        confirmation += f"\nWarning: {json.dumps(duplicate_warning, indent=2)}"
    return confirmation


def file_queued(content: str, wing: str, room: str) -> str:
    """Team-mode file (verbatim drawer). No dedupe; straight to queue."""
    started = time.perf_counter()
    did, filepath = vault.write_drawer(content, wing, room, committed=False)

    r = _redis()
    r.xadd(STREAM_KEY, {"kind": "drawer", "id": did, "action": "upsert"})

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.observe("write_latency_ms", elapsed_ms, {"tool": "file", "mode": "team"})
    metrics.incr("write_total", {"tool": "file", "mode": "team", "outcome": "enqueued"})

    return f"Filed (queued): {did} → {wing}/{room}"


def diary_write_queued(content: str, session_id: str) -> str:
    """Team-mode diary_write.

    Diary entries append to a per-day file. The team worker re-indexes the
    whole day on every commit, so multiple enqueues for the same day are
    idempotent (last write determines what's in chroma)."""
    started = time.perf_counter()
    filepath = vault.diary_write(content, session_id)
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    r = _redis()
    r.xadd(STREAM_KEY, {"kind": "diary", "id": f"diary_{today}", "action": "upsert"})

    elapsed_ms = (time.perf_counter() - started) * 1000
    metrics.observe("write_latency_ms", elapsed_ms, {"tool": "diary_write", "mode": "team"})
    metrics.incr("write_total", {"tool": "diary_write", "mode": "team", "outcome": "enqueued"})

    return f"Diary entry enqueued: {filepath}"
