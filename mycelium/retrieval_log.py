"""JSONL log of every read-path retrieval call.

Baseline data for the scope-aware-retrieval / user-poisoning work
(strategy note: note_86ea3fb995350faa). Captures, for every call to a
retrieval MCP tool: the query, the tool's params, latency, the
caller (agent vs hook), and the wings/rooms/titles of every returned
entity. Lets us answer "how often does retrieval cross wings, today,
before any scope-aware retrieval is implemented" and "is the
UserPromptSubmit hook surfacing relevant titles" without changing
how the agent sees results.

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


def parallel_fanout(*calls):
    """Run N zero-arg callables in parallel, with suppress_nested active
    in each worker thread.

    Used by context() and context_titles() to fan out their three sub-
    fetches (notes / drawers / links) across a ThreadPoolExecutor instead
    of running them sequentially. Each Loki/chroma call is independent;
    parallelism cuts wall-clock latency by ~2-3× when the chroma queries
    are the dominant cost.

    Each `call` is a zero-arg callable (lambda or functools.partial).
    Returns results in the same order as calls.

    Threads don't auto-inherit the caller's ContextVar context, so each
    worker explicitly sets `_suppress = True` before running its callable.
    """
    import concurrent.futures

    def _wrapped(call):
        token = _suppress.set(True)
        try:
            return call()
        finally:
            _suppress.reset(token)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(calls)) as ex:
        futures = [ex.submit(_wrapped, c) for c in calls]
        return tuple(f.result() for f in futures)


# Drawer snippet length in the log — short enough to keep records compact,
# long enough that a human reading the log gets the gist of the match.
_SNIPPET_CHARS = 100


def _drawer_snippet(d: dict[str, Any]) -> str:
    raw = d.get("text") or d.get("snippet") or ""
    return raw.strip().replace("\n", " ")[:_SNIPPET_CHARS]


def _entity_from_note(n: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "note",
        "id": n.get("note_id"),
        "title": n.get("title"),
        "distance": n.get("distance"),
    }


def _entity_from_drawer(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "drawer",
        "id": d.get("drawer_id"),
        "wing": d.get("wing"),
        "room": d.get("room"),
        "snippet": _drawer_snippet(d),
        "distance": d.get("distance"),
    }


def _entity_from_link(l: dict[str, Any]) -> dict[str, Any]:
    src = l.get("source") or {}
    tgt = l.get("target") or {}
    return {
        "type": "link",
        "id": l.get("link_id"),
        "source_id": src.get("id"),
        "source_label": src.get("label"),
        "target_id": tgt.get("id"),
        "target_label": tgt.get("label"),
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
    caller: str = "agent",
    latency_ms: float | None = None,
) -> None:
    """Append one JSONL record describing a retrieval call.

    Silently no-ops if suppressed, the log path is empty, or the write
    fails. Never raises — retrieval logging must not break retrieval.
    """
    if _suppress.get():
        return
    if not RETRIEVAL_LOG_PATH:
        return

    notes_list = list(notes or ())
    drawers_list = list(drawers or ())
    links_list = list(links or ())

    returned: list[dict[str, Any]] = []
    returned.extend(_entity_from_note(n) for n in notes_list)
    returned.extend(_entity_from_drawer(d) for d in drawers_list)
    returned.extend(_entity_from_link(l) for l in links_list)

    wings_returned = sorted({
        e["wing"] for e in returned if e.get("type") == "drawer" and e.get("wing")
    })

    n_by_source = {
        "notes": len(notes_list),
        "drawers": len(drawers_list),
        "links": len(links_list),
    }

    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool,
        "caller": caller,
        "query": query,
        "params": params or {},
        "latency_ms": round(latency_ms, 2) if latency_ms is not None else None,
        "n_returned": len(returned),
        "n_returned_by_source": n_by_source,
        "empty_result": len(returned) == 0,
        # Scalar mirror of len(wings_returned) for cheap LogQL/Grafana
        # aggregation (counting array length in LogQL is hairy; scalar
        # is free at emission time and trivial to query).
        "n_wings_returned": len(wings_returned),
        "cross_wing": len(wings_returned) > 1,
        # String mirror of wings_returned for LogQL: Loki's `| json` parser
        # extracts scalars but drops arrays, so the array form isn't
        # queryable. Joining with "+" gives a single label value like
        # "ipcore+systems" that powers the wing-co-occurrence barchart.
        "wings_returned_str": "+".join(wings_returned),
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
