"""JSONL log of every read-path retrieval call.

Baseline data for the scope-aware-retrieval / user-poisoning work
(strategy note: note_86ea3fb995350faa). Captures, for every call to a
retrieval MCP tool: the query, the tool's params, and the wings/rooms
of every returned drawer. Lets us answer "how often does retrieval
cross wings, today, before any scope-aware retrieval is implemented"
without changing how the agent sees results.

Designed to be cheap and non-blocking — write failures are swallowed
with a warning, never raised to the caller. Disabled by setting
MYCELIUM_RETRIEVAL_LOG to an empty string.

Nested-call suppression: context() internally calls query_notes() and
find_links(). The wrapper `suppress_nested()` context manager lets the
outer call own the log line so we don't double-count.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from mycelium.config import RETRIEVAL_LOG_PATH

_logger = logging.getLogger(__name__)

_suppress: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mycelium_retrieval_log_suppress", default=False
)


@contextlib.contextmanager
def suppress_nested():
    """Within this block, log_retrieval() is a no-op.

    Used by context() to silence its internal calls to query_notes()
    and find_links() — the outer context() call writes one combined
    log line covering all of them.
    """
    token = _suppress.set(True)
    try:
        yield
    finally:
        _suppress.reset(token)


def _entity_from_note(n: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "note",
        "id": n.get("note_id"),
        "distance": n.get("distance"),
    }


def _entity_from_drawer(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "drawer",
        "id": d.get("drawer_id"),
        "wing": d.get("wing"),
        "room": d.get("room"),
        "distance": d.get("distance"),
    }


def _entity_from_link(l: dict[str, Any]) -> dict[str, Any]:
    src = l.get("source") or {}
    tgt = l.get("target") or {}
    return {
        "type": "link",
        "id": l.get("link_id"),
        "source_id": src.get("id"),
        "target_id": tgt.get("id"),
        "relation_type": l.get("relation_type"),
        "distance": l.get("distance"),
    }


def log_retrieval(
    tool: str,
    query: str,
    params: dict[str, Any] | None = None,
    notes: Iterable[dict[str, Any]] | None = None,
    drawers: Iterable[dict[str, Any]] | None = None,
    links: Iterable[dict[str, Any]] | None = None,
) -> None:
    """Append one JSONL record describing a retrieval call.

    Silently no-ops if suppressed, the log path is empty, or the write
    fails. Never raises — retrieval logging must not break retrieval.
    """
    if _suppress.get():
        return
    if not RETRIEVAL_LOG_PATH:
        return

    returned: list[dict[str, Any]] = []
    if notes:
        returned.extend(_entity_from_note(n) for n in notes)
    if drawers:
        returned.extend(_entity_from_drawer(d) for d in drawers)
    if links:
        returned.extend(_entity_from_link(l) for l in links)

    wings_returned = sorted({
        e["wing"] for e in returned if e.get("type") == "drawer" and e.get("wing")
    })

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "query": query,
        "params": params or {},
        "n_returned": len(returned),
        "wings_returned": wings_returned,
        "returned": returned,
    }

    try:
        path = Path(RETRIEVAL_LOG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")
    except Exception as e:
        _logger.warning("retrieval log write failed: %s", e)
