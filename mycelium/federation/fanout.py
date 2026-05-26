"""Federated `context()` — parallel fanout + rank fusion."""
from __future__ import annotations

import asyncio
import time

from mycelium import config, metrics
from mycelium.federation import budgets, sources


DEFAULT_K_PER_SOURCE = 5
DEFAULT_N_RESULTS   = 20


def _bias_for(source_name: str) -> float:
    """Resolve the per-source rank bias.

    Built-in mycelium sources use the existing config constants
    (mycelium#14). External sources read from EXTERNAL_SOURCE_BIASES
    in ~/.mycelium/config.json, defaulting to 0.0 if not configured.
    """
    builtin = {
        "mycelium-notes":   config.SOURCE_BIAS_NOTE,
        "mycelium-drawers": config.SOURCE_BIAS_DRAWER,
        "mycelium-links":   config.SOURCE_BIAS_LINK,
    }
    if source_name in builtin:
        return builtin[source_name]
    return config.EXTERNAL_SOURCE_BIASES.get(source_name, 0.0)


async def federated_context(
    query: str,
    sources_filter: list[str] | None = None,
    k_per_source: int = DEFAULT_K_PER_SOURCE,
    n_results: int = DEFAULT_N_RESULTS,
) -> dict:
    """Fan out to selected sources, rank-fuse top-N, return ranked list.

    Args:
        query: user query
        sources_filter: list of adapter names; None or ["all"] = all registered
        k_per_source: top-K each source returns before merge
        n_results: final cap on merged result count
    """
    if sources_filter is None or sources_filter == ["all"]:
        targets = sources.all_names()
    else:
        targets = [n for n in sources_filter if sources.get(n) is not None]

    async def _one(name: str) -> tuple[str, list, float]:
        start = time.perf_counter()
        adapter = sources.get(name)
        if adapter is None:
            return name, [], 0.0
        try:
            results = await adapter.search(query, k_per_source)
        except Exception:
            metrics.incr("read_error_total", {"source": name})
            return name, [], (time.perf_counter() - start) * 1000.0
        return name, results, (time.perf_counter() - start) * 1000.0

    gathered = await asyncio.gather(*[_one(n) for n in targets])

    merged: list[dict] = []
    diagnostics: dict = {}
    for name, results, took_ms in gathered:
        diagnostics[name] = {"hits": len(results), "took_ms": round(took_ms, 1)}
        bias = _bias_for(name)
        for r in results:
            merged.append({
                "source":         r.source,
                "rank":           r.rank,
                "effective_rank": r.rank - bias,
                "id":             r.id,
                "title":          r.title,
                "snippet":        r.snippet,
                "metadata":       r.metadata,
            })
        metrics.incr("read_total", {"source": name}, value=float(len(results)))
        metrics.observe("read_latency_ms", took_ms, {"source": name})

    merged.sort(key=lambda r: r["effective_rank"], reverse=True)
    top = merged[:n_results]

    capped, truncation_count = budgets.cap_payload(top)

    return {
        "query": query,
        "results": capped,
        "source_diagnostics": diagnostics,
        "truncated": truncation_count,
    }
