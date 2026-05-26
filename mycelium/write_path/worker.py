"""Team-mode writer worker.

Single process (no leader election in v1.1.0 — restart-on-crash via k8s
RestartPolicy=Always or systemd is sufficient for the personal/team-scale
write rate). Consumes the `mycelium:writes` stream, performs ChromaDB
upserts, flips the `committed_at` frontmatter flag, ACKs.

Run via: `mycelium serve --worker` (see cli.py).

GC sweep on startup: scans the vault for `committed_at: null` files older
than 10 minutes and re-enqueues them, recovering from any prior crash that
left orphaned drafts on disk.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from mycelium import config, metrics, vault

STREAM_KEY = "mycelium:writes"
GROUP_NAME = "mycelium:writers"
CONSUMER_NAME = "worker-0"

GC_THRESHOLD_SECONDS = 600       # 10 min — files older than this with
                                  # committed_at: null get re-enqueued
BLOCK_MS = 5000                   # XREADGROUP block timeout


def _redis():
    try:
        import redis as redis_module
    except ImportError as e:
        raise RuntimeError(
            "Worker requires redis-py. Install with: uv pip install mycelium-palace[team]"
        ) from e
    return redis_module.Redis.from_url(
        getattr(config, "REDIS_URL", "redis://localhost:6379/0")
    )


def _ensure_group(r) -> None:
    """Create the consumer group if it doesn't exist. BUSYGROUP = already exists."""
    try:
        r.xgroup_create(STREAM_KEY, GROUP_NAME, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise


def _gc_sweep_on_startup(r) -> int:
    """Scan vault for orphaned `committed_at: null` files older than threshold.

    Re-enqueues them onto the stream so the worker reprocesses them.
    Returns count of re-enqueued items.
    """
    count = 0
    cutoff = time.time() - GC_THRESHOLD_SECONDS

    # Notes
    if config.NOTES_DIR.exists():
        for path in config.NOTES_DIR.glob("*.md"):
            if _is_pending(path) and path.stat().st_mtime < cutoff:
                note = vault.load_note(path)
                if note:
                    r.xadd(STREAM_KEY, {
                        "kind": "note", "id": note["note_id"], "action": "upsert",
                    })
                    count += 1

    # Drawers
    if config.DRAWERS_DIR.exists():
        for path in config.DRAWERS_DIR.glob("*.md"):
            if _is_pending(path):
                drawer = vault.get_drawer(path.stem)
                if drawer and path.stat().st_mtime < cutoff:
                    r.xadd(STREAM_KEY, {
                        "kind": "drawer", "id": drawer["drawer_id"], "action": "upsert",
                    })
                    count += 1

    metrics.incr("gc_reenqueued_total", value=float(count))
    if count:
        print(f"[worker] GC sweep re-enqueued {count} orphaned drafts")
    return count


def _is_pending(path: Path) -> bool:
    try:
        return frontmatter.load(str(path)).get("committed_at") is None
    except Exception:
        return False


def _oldest_pending_age() -> float:
    """Age in seconds of the oldest committed_at: null file. 0 if none."""
    oldest = None
    for dir_ in (config.NOTES_DIR, config.DRAWERS_DIR):
        if not dir_.exists():
            continue
        for path in dir_.glob("*.md"):
            if _is_pending(path):
                mtime = path.stat().st_mtime
                if oldest is None or mtime < oldest:
                    oldest = mtime
    if oldest is None:
        return 0.0
    return time.time() - oldest


def _commit_one(r, msg_id, payload: dict) -> None:
    """Process one stream message: read disk file → upsert chroma → flip flag → ACK."""
    kind = payload.get("kind", "").decode() if isinstance(payload.get("kind"), bytes) else payload.get("kind", "")
    item_id = payload.get("id", "").decode() if isinstance(payload.get("id"), bytes) else payload.get("id", "")
    action = payload.get("action", "upsert")
    if isinstance(action, bytes):
        action = action.decode()

    if kind == "note":
        path = config.NOTES_DIR / _slug_from_note_id(item_id)
        if not path.exists():
            metrics.incr("writer_skipped_total")
            r.xack(STREAM_KEY, GROUP_NAME, msg_id)
            return
        if action == "upsert":
            _upsert_note_from_disk(item_id, path)
        elif action == "delete":
            _delete_note(item_id)
    elif kind == "drawer":
        path = config.DRAWERS_DIR / f"{item_id}.md"
        if not path.exists():
            metrics.incr("writer_skipped_total")
            r.xack(STREAM_KEY, GROUP_NAME, msg_id)
            return
        if action == "upsert":
            _upsert_drawer_from_disk(item_id, path)
        elif action == "delete":
            _delete_drawer(item_id)
    elif kind == "diary":
        date_str = item_id.replace("diary_", "")
        path = config.DIARY_DIR / f"{date_str}.md"
        if path.exists():
            vault._index_diary_day(date_str, path)
    else:
        # Unknown kind — log and ack to avoid stuck stream.
        metrics.incr("writer_skipped_total")

    # Stamp committed_at on the file (for note + drawer; diary skipped).
    if kind in ("note", "drawer") and action == "upsert" and path.exists():
        post = frontmatter.load(str(path))
        post["committed_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

    r.xack(STREAM_KEY, GROUP_NAME, msg_id)
    metrics.incr("commit_total", {"kind": kind})


def _slug_from_note_id(note_id: str) -> str:
    """Look up the file path for a note by ID. Notes are saved as <slug>.md
    so we need to scan to find the right one."""
    if not config.NOTES_DIR.exists():
        return f"{note_id}.md"   # placeholder; existence check upstream
    for path in config.NOTES_DIR.glob("*.md"):
        try:
            if frontmatter.load(str(path)).get("id") == note_id:
                return path.name
        except Exception:
            pass
    return f"{note_id}.md"   # fallback; will fail existence check


def _upsert_note_from_disk(nid: str, path: Path) -> None:
    """Read note from disk + upsert into chroma. Same metadata shape as direct.py."""
    import json as _json
    post = frontmatter.load(str(path))
    title = post.get("title", "")
    tags = post.get("tags", []) or []
    source_memories = post.get("source_memories", []) or []
    status = post.get("status", "")

    metadata: dict = {
        "title":           title,
        "tags":            _json.dumps(tags),
        "source_memories": _json.dumps(source_memories),
        "created_at":      post.get("created", ""),
        "filepath":        str(path),
    }
    if status:
        metadata["status"] = status

    from mycelium.chroma import notes_collection
    notes_collection().upsert(
        ids=[nid],
        documents=[f"{title}\n\n{post.content}"],
        metadatas=[metadata],
    )


def _upsert_drawer_from_disk(did: str, path: Path) -> None:
    """Read drawer from disk + upsert into chroma."""
    post = frontmatter.load(str(path))
    wing = post.get("wing", "unknown")
    room = post.get("room", "unknown")
    filed_at = post.get("filed_at", "")

    from mycelium.chroma import drawers_collection
    drawers_collection().upsert(
        ids=[did],
        documents=[post.content],
        metadatas=[{
            "drawer_id":   did,
            "wing":        wing,
            "room":        room,
            "filed_at":    filed_at,
            "source_file": str(path),
        }],
    )


def _delete_note(nid: str) -> None:
    from mycelium.chroma import notes_collection
    try:
        notes_collection().delete(ids=[nid])
    except Exception:
        pass


def _delete_drawer(did: str) -> None:
    from mycelium.chroma import drawers_collection
    try:
        drawers_collection().delete(ids=[did])
    except Exception:
        pass


def run() -> None:
    """Main worker loop. Blocks on XREADGROUP, processes messages forever."""
    r = _redis()
    _ensure_group(r)
    _gc_sweep_on_startup(r)
    metrics.incr("writer_restart_total")

    print(f"[worker] mycelium writer started, consuming from {STREAM_KEY}")

    while True:
        try:
            entries = r.xreadgroup(
                GROUP_NAME, CONSUMER_NAME,
                {STREAM_KEY: ">"},
                count=10, block=BLOCK_MS,
            )
            for _stream, items in entries or []:
                for msg_id, payload in items:
                    _commit_one(r, msg_id, payload)
            # Update gauges (cheap; runs every BLOCK_MS regardless of activity).
            metrics.set_gauge("queue_xlen", float(r.xlen(STREAM_KEY)))
            metrics.set_gauge("oldest_pending_age_seconds", _oldest_pending_age())
        except KeyboardInterrupt:
            print("[worker] shutdown requested")
            break
        except Exception as e:
            metrics.incr("writer_error_total")
            print(f"[worker] error in main loop: {e!r}")
            time.sleep(1)
