"""Hybrid BM25 + vector search over mycelium drawers.

Forked from mempalace/searcher.py. Adapted for mycelium's ChromaDB schema
(single chroma dir, mycelium_drawers collection, different metadata fields).

Search strategy: vector retrieval over-fetches (3x), then BM25 re-ranks.
This catches keyword matches that vector search scores low due to embedding
noise on mechanical/technical content.
"""
from __future__ import annotations

import math
import re
from typing import Any

_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _bm25_scores(
    query: str,
    documents: list[str],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    n_docs = len(documents)
    query_terms = set(_tokenize(query))
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs

    tokenized = [_tokenize(d) for d in documents]
    doc_lens = [len(toks) for toks in tokenized]
    if not any(doc_lens):
        return [0.0] * n_docs
    avgdl = sum(doc_lens) / n_docs or 1.0

    df: dict[str, int] = {term: 0 for term in query_terms}
    for toks in tokenized:
        for term in set(toks) & query_terms:
            df[term] += 1

    idf = {
        term: math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1)
        for term in query_terms
    }

    scores = []
    for toks, dl in zip(tokenized, doc_lens):
        if dl == 0:
            scores.append(0.0)
            continue
        tf: dict[str, int] = {}
        for t in toks:
            if t in query_terms:
                tf[t] = tf.get(t, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            num = freq * (k1 + 1)
            den = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf[term] * num / den
        scores.append(score)
    return scores


def hybrid_rerank(
    hits: list[dict[str, Any]],
    query: str,
    vector_weight: float = 0.6,
    bm25_weight: float = 0.4,
) -> list[dict[str, Any]]:
    """Re-rank hits by convex combination of vector similarity and BM25.

    Each hit must have "text" (str) and "distance" (float, cosine).
    Mutates hits in place and returns them sorted best-first.
    """
    if not hits:
        return hits

    docs = [h.get("text", "") for h in hits]
    bm25_raw = _bm25_scores(query, docs)
    max_bm25 = max(bm25_raw) if bm25_raw else 0.0
    bm25_norm = [s / max_bm25 for s in bm25_raw] if max_bm25 > 0 else [0.0] * len(bm25_raw)

    scored = []
    for h, raw, norm in zip(hits, bm25_raw, bm25_norm):
        vec_sim = max(0.0, 1.0 - h.get("distance", 1.0))
        h["bm25_score"] = round(raw, 3)
        scored.append((vector_weight * vec_sim + bm25_weight * norm, h))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    hits[:] = [h for _, h in scored]
    return hits


_CLOSET_DRAWER_REF_RE = re.compile(r"→([\w,]+)")

# Mempalace-derived ranking knobs. Closet rank → boost on drawer's effective
# distance. Lower-rank closet hits boost more (better topical match). Cap at
# distance 1.5 — closets weaker than that aren't trustworthy signal.
CLOSET_RANK_BOOSTS = [0.40, 0.25, 0.15, 0.08, 0.04]
CLOSET_DISTANCE_CAP = 1.5


def _closet_boosts(query: str, n_closets: int) -> dict[str, tuple[int, float, str]]:
    """Query closets collection, return {drawer_id: (rank, distance, preview)}.

    For each closet that ranks well, parse its referenced drawer_ids (encoded
    in the closet document text as "→drawer_id_a,drawer_id_b") and stamp each
    one with the closet's rank.

    First match per drawer_id wins (best closet rank for that drawer).
    """
    from mycelium.chroma import closets_collection

    col = closets_collection()
    if col.count() == 0:
        return {}

    try:
        results = col.query(
            query_texts=[query],
            n_results=min(n_closets, col.count()),
            include=["documents", "distances"],
        )
    except Exception:
        return {}

    docs  = results["documents"][0] if results["documents"] else []
    dists = results["distances"][0] if results["distances"] else []

    boost_by_drawer: dict[str, tuple[int, float, str]] = {}
    for rank, (doc, dist) in enumerate(zip(docs, dists)):
        if dist > CLOSET_DISTANCE_CAP:
            continue
        preview = (doc or "")[:200]
        for match in _CLOSET_DRAWER_REF_RE.findall(doc or ""):
            for did in match.split(","):
                did = did.strip()
                if did and did not in boost_by_drawer:
                    boost_by_drawer[did] = (rank, dist, preview)
    return boost_by_drawer


def search_drawers(
    query: str,
    wing: str | None = None,
    room: str | None = None,
    n_results: int = 10,
    max_distance: float = 0.0,
) -> dict[str, Any]:
    """Hybrid BM25 + vector search with optional closet-boost over drawers.

    Three-stage ranking:
      1. Vector retrieval (over-fetch 3x) on the drawers collection.
      2. Closet boost: if a drawer is referenced by a query-matching closet,
         shrink its effective distance by a rank-based amount. Closet hits
         are a signal, never a gate — drawers without closet coverage still
         compete on raw similarity.
      3. BM25 hybrid re-rank within the candidate set so keyword matches
         aren't drowned out by vector noise on technical content.

    Args:
        query: Natural language search query.
        wing: Optional wing filter (e.g. "mycelium", "rain").
        room: Optional room filter (e.g. "decisions", "code").
        n_results: Max results to return (default 10).
        max_distance: Cosine distance threshold. 0.0 = no filter. Typical: 0.3-0.85.

    Returns:
        Dict with "query", "filters", "total_before_filter", "results".
    """
    from mycelium.chroma import drawers_collection

    col = drawers_collection()
    count = col.count()
    if count == 0:
        return {"query": query, "filters": {"wing": wing, "room": room}, "total_before_filter": 0, "results": []}

    where: dict | None = None
    if wing and room:
        where = {"$and": [{"wing": wing}, {"room": room}]}
    elif wing:
        where = {"wing": wing}
    elif room:
        where = {"room": room}

    fetch = min(n_results * 3, count)
    kwargs: dict[str, Any] = {
        "query_texts": [query],
        "n_results": fetch,
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        kwargs["where"] = where

    try:
        results = col.query(**kwargs)
    except Exception as e:
        return {"query": query, "error": str(e)}

    docs = results["documents"][0] if results["documents"] else []
    metas = results["metadatas"][0] if results["metadatas"] else []
    dists = results["distances"][0] if results["distances"] else []

    # Closet boosts — drawer_id → (rank, dist, preview)
    boost_by_drawer = _closet_boosts(query, n_closets=n_results * 2)

    hits = []
    for doc, meta, dist in zip(docs, metas, dists):
        if max_distance > 0.0 and dist > max_distance:
            continue
        meta = meta or {}
        did = meta.get("drawer_id", "")

        boost = 0.0
        matched_via = "drawer"
        closet_preview: str | None = None
        if did in boost_by_drawer:
            c_rank, _c_dist, c_preview = boost_by_drawer[did]
            if c_rank < len(CLOSET_RANK_BOOSTS):
                boost = CLOSET_RANK_BOOSTS[c_rank]
                matched_via = "drawer+closet"
                closet_preview = c_preview

        effective_dist = dist - boost

        entry = {
            "text":               doc or "",
            "wing":               meta.get("wing", "unknown"),
            "room":               meta.get("room", "unknown"),
            "drawer_id":          did,
            "source_file":        meta.get("source_file", "?"),
            "filed_at":           meta.get("filed_at", ""),
            "author":             meta.get("author", ""),
            "similarity":         round(max(0.0, 1 - effective_dist), 3),
            "distance":           round(dist, 4),
            "effective_distance": round(effective_dist, 4),
            "closet_boost":       round(boost, 3),
            "matched_via":        matched_via,
            "_sort_key":          effective_dist,
        }
        if closet_preview:
            entry["closet_preview"] = closet_preview
        hits.append(entry)

    hits.sort(key=lambda h: h["_sort_key"])

    # Dedupe by drawer_id — keep the best-ranked chunk per parent drawer so a
    # multi-chunk document doesn't crowd out other relevant drawers in the
    # top-N. The chunk_index metadata is preserved so the agent knows which
    # part of the drawer matched.
    seen: set[str] = set()
    deduped: list[dict] = []
    for h in hits:
        did = h.get("drawer_id", "")
        if did and did in seen:
            continue
        if did:
            seen.add(did)
        deduped.append(h)

    deduped = deduped[:n_results]
    for h in deduped:
        h.pop("_sort_key", None)
    hits = deduped

    hybrid_rerank(hits, query)

    return {
        "query":               query,
        "filters":             {"wing": wing, "room": room},
        "total_before_filter": len(docs),
        "results":             hits,
    }
