"""Mycelium MCP server — single process, disk-first, no external memory service.

Tool surface (Phase 1 — all tools visible to main agent):
  Context:   context
  Drawers:   file, get_drawer, list_drawers, list_rooms, list_wings, check_duplicate, update_drawer, delete_drawer
  Notes:     write_note, query_notes
  Links:     add_link, query_links, find_links, delete_link
  Diary:     diary_write, diary_read
  Utility:   search, status

Phase 2 adds ask_memory (Haiku subagent) and narrows main agent to 4 tools.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

import frontmatter
from fastmcp import FastMCP

from mycelium.chroma import drawers_collection, links_collection, notes_collection
from mycelium.config import (
    NOTES_DIR,
    SOURCE_BIAS_DRAWER,
    SOURCE_BIAS_LINK,
    SOURCE_BIAS_NOTE,
    VAULT_DIR,
)
from mycelium.retrieval_log import log_retrieval, parallel_fanout, suppress_nested
from mycelium.search import search_drawers
from mycelium.write_log import log_write
from mycelium.vault import (
    chunk_content as _chunk_content,
    delete_drawer as _vault_delete_drawer,
    delete_link_file as _vault_delete_link_file,
    delete_note as _vault_delete_note,
    diary_read as _vault_diary_read,
    diary_write as _vault_diary_write,
    get_drawer as _vault_get_drawer,
    link_id as _link_id_fn,
    list_drawers as _vault_list_drawers,
    list_rooms as _vault_list_rooms,
    list_wings as _vault_list_wings,
    load_note as _vault_load_note,
    note_id as _note_id_fn,
    slugify as _slugify,
    update_closet_for_drawer as _vault_update_closet_for_drawer,
    update_drawer as _vault_update_drawer,
    write_drawer as _vault_write_drawer,
    write_link as _vault_write_link,
    write_note as _vault_write_note,
)

mcp = FastMCP(
    "mycelium",
    instructions=(
        "Mycelium is a unified memory system spanning sessions and clients: verbatim captures (drawers), "
        "curated notes, and typed links between them.\n"
        "\n"
        "WHEN TO CALL context():\n"
        "1. At the start of the first substantive exchange in a session — before searching the filesystem "
        "or answering from memory.\n"
        "2. Before starting any non-trivial task or answering a technical / project question.\n"
        "3. When the user references prior work, a project name, a system, or says 'last time / remember "
        "when / we were working on'.\n"
        "\n"
        "FALLBACK: if context() output is too large or the tool is unavailable, use query_notes() or "
        "search() with a precise topic. Search broadly — do not restrict to a specific wing unless you "
        "have already explored the taxonomy.\n"
        "\n"
        "FEDERATED context (when external sources are configured): use context_federated(query, sources=...) "
        "to fan out across mycelium + external sources (gitlab, backstage, rain-docs, zabbix as they land) "
        "with rank fusion. Pass sources=['all'] to hit every registered adapter; pass a specific list to "
        "scope (e.g. sources=['mycelium-notes', 'gitlab-config']). Returns ranked results from all sources "
        "in one call; synthesize across them yourself.\n"
        "\n"
        "CAPTURE:\n"
        "- file() for verbatim content (user paste, log output, transcript).\n"
        "- write_note(title, content, tags, intent) for settled conclusions and decisions. "
        "INTENT IS REQUIRED (v2.0.0+) — pass a non-empty string explaining WHY this note is being written "
        "(e.g. 'capturing the deployment decision from today's planning meeting'). It's stamped as "
        "source_intent in frontmatter so future searches see why the note exists.\n"
        "- Before filing into an unfamiliar wing/room, call get_taxonomy() once to see existing names — "
        "avoid fragmenting with near-duplicates (e.g. 'decisions' vs 'decision').\n"
        "\n"
        "LINKS:\n"
        "- When results include a note_id or drawer_id, use query_links(entity_id) to traverse the graph "
        "and discover connected entities.\n"
        "- Use add_link() whenever you notice two entities that are clearly related but have no link "
        "between them. Links may connect any combination of notes / drawers / concepts — drawer↔drawer "
        "and drawer↔note links are first-class, not just note↔note.\n"
        "\n"
        "Drawers are the source of truth for raw content; notes are authoritative for curated decisions."
    ),
)


# Load federation adapters from the user's allowlist. Each adapter registers
# itself via register() at import time; the federated context_federated()
# tool reads from that registry.
try:
    from mycelium.federation import load_adapters as _load_federation_adapters
    _load_federation_adapters()
except Exception:
    # Don't let a bad adapter import crash the server startup.
    pass


# ---------------------------------------------------------------------------
# Context (primary entry point)
# ---------------------------------------------------------------------------

# Per-tool-result size budget. Claude Code's hard cap sits around 50 KB; this
# threshold leaves ~10 KB margin for JSON formatting / response envelope so
# even a tight-to-the-limit auto-downgrade still lands safely.
_CONTEXT_SIZE_LIMIT = 40_000

# Per-item content cap in snippet mode.
_SNIPPET_MAX_CHARS = 500

# Fields preserved in titles mode (drops all bulk content).
_TITLES_KEEP_FIELDS = frozenset({
    "note_id", "drawer_id", "link_id", "from_entity",
    "title", "wing", "room",
    "tags", "filepath", "source_file",
    "source", "target", "relation_type",
    "source_type",  # top_results entry: "note" | "drawer" | "link"
    "distance", "similarity", "effective_distance",
})


def _snippet_item(item: dict, max_chars: int = _SNIPPET_MAX_CHARS) -> dict:
    """Truncate long string fields to max_chars; keep IDs and metadata intact."""
    out: dict = {}
    for k, v in item.items():
        if isinstance(v, str) and len(v) > max_chars:
            out[k] = v[:max_chars] + "…[truncated]"
        else:
            out[k] = v
    return out


def _titles_item(item: dict) -> dict:
    """Drop bulk content; keep only identity, location, and ranking fields."""
    return {k: v for k, v in item.items() if k in _TITLES_KEEP_FIELDS}


def _degrade_result(result: dict, level: Literal["snippet", "titles"]) -> dict:
    """Apply the snippet- or titles-level transformation to a context() result.

    Mutates only the list values under known keys; preserves other top-level keys.
    Returns a new dict (shallow copy at top, transformed lists).
    """
    transform = _snippet_item if level == "snippet" else _titles_item
    out = dict(result)
    for key in ("notes", "memories", "links", "links_from_results"):
        if isinstance(out.get(key), list):
            out[key] = [transform(item) for item in out[key]]
    out["_degraded_to"] = level
    return out


@mcp.tool()
def context(
    query: str,
    n_notes: int = 10,
    n_drawers: int = 10,
    n_links: int = 10,
    max_distance: float = 0.75,
    expand_links: bool = True,
    mode: Literal["auto", "full", "snippet", "titles"] = "auto",
    _caller: str = "agent",
) -> str:
    """Retrieve combined context: curated notes + verbatim drawers + related links.

    The primary tool for task start. Returns all three layers in one call, plus
    optionally the graph neighborhood — outgoing links from any returned entity
    so the agent sees connected content without a second tool call.

    Output size is automatically capped: in `auto` mode (default), the tool
    measures the full payload and downgrades to snippets (~500 chars per item)
    or titles-only if the response would exceed Claude Code's per-tool-result
    limit. The response includes `_degraded_to` ("snippet" or "titles") when
    a downgrade was applied so the agent can re-fetch specific items in full
    with get_drawer() or query_notes() if needed.

    Args:
        query: What you are looking for.
        n_notes: Max curated notes (default 10).
        n_drawers: Max verbatim drawers (default 10).
        n_links: Max links found via semantic search on link descriptions (default 10).
        max_distance: Cosine distance ceiling for drawer results (default 0.75).
        expand_links: If True (default), also include outgoing links from each
            returned note/drawer under "links_from_results". Provides the
            graph neighborhood of the matched entities.
        mode: Output verbosity. "auto" (default) returns full content but
            downgrades to snippet then titles if the response would exceed
            the ~40 KB tool-output budget. "full" always returns full content
            (use with smaller n_*). "snippet" forces snippet truncation.
            "titles" returns only identity/ranking fields, no content.

    Returns:
        JSON with notes, memories (drawers), links (semantic on descriptions),
        and (if expand_links) links_from_results (graph edges from matches).
        Also includes `top_results` — a lightweight merged ranking across all
        sources with per-source bias applied (notes outrank drawers outrank
        links at similar raw distance). Includes `_degraded_to` when
        auto-degradation was applied to the bulk per-source lists.
    """
    _t0 = time.perf_counter()
    # Fan out the three independent chroma queries (notes / drawers / links)
    # concurrently rather than sequentially — cuts p95 wall-clock significantly
    # when chroma roundtrips dominate.
    notes_str, drawer_result, links_str = parallel_fanout(
        lambda: query_notes(query, n_notes),
        lambda: search_drawers(query, n_results=n_drawers, max_distance=max_distance),
        lambda: find_links(query, n_links),
    )
    notes   = json.loads(notes_str).get("notes", [])
    drawers = drawer_result.get("results", [])
    links   = json.loads(links_str).get("links", [])

    log_retrieval(
        tool="context",
        query=query,
        params={
            "n_notes": n_notes,
            "n_drawers": n_drawers,
            "n_links": n_links,
            "max_distance": max_distance,
            "expand_links": expand_links,
            "mode": mode,
        },
        notes=notes,
        drawers=drawers,
        links=links,
        caller=_caller,
        latency_ms=(time.perf_counter() - _t0) * 1000,
    )

    result: dict = {"query": query, "notes": notes, "memories": drawers, "links": links}

    if expand_links and (notes or drawers):
        # Pull outgoing links for each top result's entity ID, dedupe by link_id
        col = links_collection()
        if col.count() > 0:
            entity_ids: list[str] = []
            for n in notes:
                if n.get("note_id"):
                    entity_ids.append(n["note_id"])
            for d in drawers:
                if d.get("drawer_id"):
                    entity_ids.append(d["drawer_id"])

            seen_link_ids = {l.get("link_id") for l in links}
            expansion: list[dict] = []
            for eid in entity_ids:
                where = {"$and": [{"source_id": eid}, {"ended_at": ""}]}
                hits = col.get(where=where, include=["metadatas"])
                for meta in hits.get("metadatas", []):
                    lid = _link_id_fn(meta["source_id"], meta["relation_type"], meta["target_id"])
                    if lid in seen_link_ids:
                        continue
                    seen_link_ids.add(lid)
                    expansion.append({
                        "link_id":       lid,
                        "from_entity":   eid,
                        "source":        {"id": meta["source_id"], "label": meta["source_label"], "type": meta["source_type"]},
                        "relation_type": meta["relation_type"],
                        "target":        {"id": meta["target_id"], "label": meta["target_label"], "type": meta["target_type"]},
                        "description":   meta["description"],
                    })
            if expansion:
                result["links_from_results"] = expansion

    if not links and not result.get("links_from_results"):
        result["links_hint"] = "No links yet — use add_link to start building the graph."

    # Build a unified top_results ranking across all sources so the agent
    # sees "what's most relevant overall" without merging the lists itself.
    # Bias each source by SOURCE_BIAS_* (lower = higher priority): notes
    # outrank drawers outrank links at similar raw distance. Entries are
    # lightweight — id, label, source type, and distances only — so they
    # don't bloat the response. Agents follow up via get_drawer() or
    # query_notes() if they need full content for a specific entry.
    merged_candidates: list[dict] = []
    for n in notes:
        if "distance" in n:
            merged_candidates.append({
                "source_type":         "note",
                "note_id":             n.get("note_id"),
                "title":               n.get("title"),
                "distance":            n["distance"],
                "effective_distance":  round(n["distance"] + SOURCE_BIAS_NOTE, 4),
            })
    for d in drawers:
        if "distance" in d:
            merged_candidates.append({
                "source_type":         "drawer",
                "drawer_id":           d.get("drawer_id"),
                "wing":                d.get("wing"),
                "room":                d.get("room"),
                "distance":            d["distance"],
                "effective_distance":  round(d["distance"] + SOURCE_BIAS_DRAWER, 4),
            })
    for l in links:
        if "distance" in l:
            src_label = (l.get("source") or {}).get("label", "?")
            tgt_label = (l.get("target") or {}).get("label", "?")
            rel       = l.get("relation_type", "?")
            merged_candidates.append({
                "source_type":         "link",
                "link_id":             l.get("link_id"),
                "title":               f"{src_label} --[{rel}]--> {tgt_label}",
                "distance":            l["distance"],
                "effective_distance":  round(l["distance"] + SOURCE_BIAS_LINK, 4),
            })
    if merged_candidates:
        merged_candidates.sort(key=lambda x: x["effective_distance"])
        result["top_results"] = merged_candidates[:10]

    # Apply requested verbosity / auto-degradation.
    if mode == "snippet":
        result = _degrade_result(result, "snippet")
    elif mode == "titles":
        result = _degrade_result(result, "titles")
    elif mode == "auto":
        payload = json.dumps(result, indent=2)
        if len(payload) >= _CONTEXT_SIZE_LIMIT:
            snippet = _degrade_result(result, "snippet")
            payload = json.dumps(snippet, indent=2)
            if len(payload) >= _CONTEXT_SIZE_LIMIT:
                result = _degrade_result(result, "titles")
            else:
                result = snippet
    # mode == "full" → no transformation

    return json.dumps(result, indent=2)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@mcp.tool()
def search(
    query: str,
    n_results: int = 10,
    max_distance: float = 0.75,
    wing: Optional[str] = None,
    room: Optional[str] = None,
    _caller: str = "agent",
) -> str:
    """Hybrid BM25 + vector search over verbatim drawer captures.

    Args:
        query: Natural-language search query.
        n_results: Max results (default 10).
        max_distance: Cosine distance ceiling (default 0.75). Lower = stricter.
        wing: Optional wing filter (project name).
        room: Optional room filter (aspect/subdirectory).

    Returns:
        JSON with "results" list. Each result has text, wing, room, drawer_id, similarity.
    """
    _t0 = time.perf_counter()
    payload = search_drawers(query, wing=wing, room=room, n_results=n_results, max_distance=max_distance)
    log_retrieval(
        tool="search",
        query=query,
        params={
            "n_results": n_results,
            "max_distance": max_distance,
            "wing": wing,
            "room": room,
        },
        drawers=payload.get("results", []),
        caller=_caller,
        latency_ms=(time.perf_counter() - _t0) * 1000,
    )
    return json.dumps(payload, indent=2)


# ---------------------------------------------------------------------------
# Drawers (verbatim)
# ---------------------------------------------------------------------------

@mcp.tool()
def file(
    content: str,
    wing: str,
    room: str,
) -> str:
    """Save verbatim content to the vault as a drawer.

    Use for raw captures: decisions, code, config, prompts, key exchanges,
    meeting notes — anything you want stored verbatim without summarising.
    Over-file rather than under-file; storage is cheap, lost context is not.

    Args:
        content: Verbatim text content to store.
        wing: Project or domain name (e.g. "mycelium", "rain", "personal").
        room: Aspect or sub-topic (e.g. "decisions", "code", "bugs").

    Returns:
        Confirmation with drawer_id.

    In team mode (MYCELIUM_DEPLOYMENT_MODE=team), the chroma upsert is
    deferred to the writer worker; closet update still happens synchronously.
    """
    from mycelium.config import DEPLOYMENT_MODE

    if DEPLOYMENT_MODE == "team":
        from mycelium.write_path.queued import file_queued
        result = file_queued(content, wing, room)
        # Closet update is local + cheap, runs in both modes synchronously.
        try:
            from mycelium.vault import drawer_id as _did_fn
            did = _did_fn(content, wing, room)
            _vault_update_closet_for_drawer(did, wing, room, content)
        except Exception:
            did = None
        log_write("file", {
            "drawer_id": did,
            "wing": wing,
            "room": room,
            "size_bytes": len(content.encode("utf-8")),
            "mode": "team",
        })
        return result

    did, filepath = _vault_write_drawer(content, wing, room)
    _index_drawer(did, content, wing, room, str(filepath))
    try:
        _vault_update_closet_for_drawer(did, wing, room, content)
    except Exception:
        pass  # closet update is best-effort, never blocks filing
    log_write("file", {
        "drawer_id": did,
        "wing": wing,
        "room": room,
        "size_bytes": len(content.encode("utf-8")),
        "mode": "personal",
    })
    return f"Filed: {did} → {wing}/{room}"


@mcp.tool()
def get_drawer(drawer_id: str) -> str:
    """Retrieve the full content of a drawer by ID.

    Args:
        drawer_id: The drawer_xxx ID returned by search or list_drawers.
    """
    d = _vault_get_drawer(drawer_id)
    if not d:
        return json.dumps({"error": f"Drawer not found: {drawer_id}"})
    return json.dumps(d, indent=2)


@mcp.tool()
def update_drawer(drawer_id: str, content: str) -> str:
    """Update the content of an existing drawer.

    Args:
        drawer_id: The drawer_xxx ID to update.
        content: New content to replace the existing content.
    """
    filepath = _vault_update_drawer(drawer_id, content)
    if not filepath:
        return json.dumps({"error": f"Drawer not found: {drawer_id}"})
    _index_drawer_from_disk(drawer_id)
    log_write("update_drawer", {
        "drawer_id": drawer_id,
        "size_bytes": len(content.encode("utf-8")),
    })
    return f"Updated: {drawer_id}"


@mcp.tool()
def delete_drawer(drawer_id: str) -> str:
    """Delete a drawer from vault and index.

    Args:
        drawer_id: The drawer_xxx ID to delete.
    """
    if not _vault_delete_drawer(drawer_id):
        return json.dumps({"error": f"Drawer not found: {drawer_id}"})
    try:
        drawers_collection().delete(ids=[drawer_id])
    except Exception:
        pass
    log_write("delete_drawer", {"drawer_id": drawer_id})
    return f"Deleted: {drawer_id}"


@mcp.tool()
def list_wings() -> str:
    """List all wings (project/domain namespaces) that have drawers.

    Returns:
        JSON list of wing names.
    """
    return json.dumps(_vault_list_wings())


@mcp.tool()
def list_rooms(wing: str) -> str:
    """List all rooms within a wing.

    Args:
        wing: The wing name to inspect.

    Returns:
        JSON list of room names within the wing.
    """
    return json.dumps(_vault_list_rooms(wing))


@mcp.tool()
def list_drawers(
    wing: Optional[str] = None,
    room: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List recent drawers, optionally filtered by wing and/or room.

    Args:
        wing: Filter to this wing (optional).
        room: Filter to this room (optional).
        limit: Max drawers to return (default 50).

    Returns:
        JSON list with drawer_id, wing, room, filed_at, preview (first 120 chars).
    """
    return json.dumps(_vault_list_drawers(wing=wing, room=room, limit=limit), indent=2)


@mcp.tool()
def check_duplicate(
    content: str,
    wing: str,
    room: str,
    max_distance: float = 0.15,
) -> str:
    """Check if very similar content already exists before filing.

    Use before file() when you're not sure if this content is already stored.

    Args:
        content: Content you're about to file.
        wing: Wing you'd file it in.
        room: Room you'd file it in.
        max_distance: Similarity threshold (default 0.15, very strict).

    Returns:
        JSON with "duplicate" bool and "similar" list if duplicates found.
    """
    result = search_drawers(content, wing=wing, room=room, n_results=3, max_distance=max_distance)
    hits = result.get("results", [])
    return json.dumps({"duplicate": bool(hits), "similar": hits[:3]}, indent=2)


# ---------------------------------------------------------------------------
# Notes (curated)
# ---------------------------------------------------------------------------

@mcp.tool()
def write_note(
    title: str,
    content: str,
    intent: str,
    tags: Optional[list[str]] = None,
    source_memories: Optional[list[str]] = None,
    status: Optional[str] = None,
) -> str:
    """Write or update a curated synthesis note.

    Call when a stable conclusion has been reached — a decision made, a
    preference confirmed, a lesson learned, an architecture choice locked in.
    Not for rough thoughts; use file() for those.

    Written to disk as Markdown and indexed in ChromaDB. Same title always
    maps to the same ID (upsert semantics).

    Args:
        title: Short descriptive title (used as filename slug).
        content: Full Markdown. Use ## Context, ## Decision/Conclusion, ## Rationale.
        intent: REQUIRED (v2.0.0+). Non-empty string explaining WHY this note
            is being written — provenance for future search. Examples:
              "Capturing the deployment decision from today's planning meeting"
              "Documenting the rebase procedure after this morning's incident"
              "Locking in the API auth choice we landed on"
            Stamped into frontmatter as `source_intent`.
        tags: Topic tags (e.g. ["infra", "decision"]).
        source_memories: Drawer IDs this note was derived from.
        status: Optional. Set when this note represents tracked work.
            Canonical values: "open" | "in-progress" | "done" | "wont-fix" | "blocked".
            Leave unset (None) for synthesis notes that aren't work items, or
            on upsert to preserve the existing status. Pass "" to clear.

    Returns:
        Confirmation with note_id and file path. Includes a Warning line if a
        semantically similar note with a different title already exists (the
        caller may want to update that note instead of creating a parallel one,
        since write_note upserts by title).

    In team mode (MYCELIUM_DEPLOYMENT_MODE=team), the disk write happens
    synchronously and a signal is enqueued to Redis; the writer worker
    commits to ChromaDB asynchronously (~minutes lag worst-case).

    Raises:
        ValueError: if `intent` is empty or whitespace-only.
    """
    if not intent or not intent.strip():
        raise ValueError(
            "`intent` is required for write_note. Pass a non-empty string "
            "explaining why this note is being written (it gets stamped as "
            "source_intent in the note's frontmatter for future provenance). "
            "Use file() instead if you have no synthesis reasoning to attach."
        )

    from mycelium.config import DEPLOYMENT_MODE

    def _dedupe_query(q: str, n: int) -> str:
        return query_notes(q, n_results=n)

    _log_fields = {
        "note_id": _note_id_fn(_slugify(title)),
        "title": title,
        "tags": tags or [],
        "status": status,
        "n_source_memories": len(source_memories or []),
        "intent_length": len(intent or ""),
        "body_length": len(content.encode("utf-8")),
    }

    if DEPLOYMENT_MODE == "team":
        from mycelium.write_path.queued import write_note_queued
        result = write_note_queued(
            title, content, tags, source_memories, status,
            intent=intent,
            query_notes_fn=_dedupe_query,
        )
        log_write("write_note", {**_log_fields, "mode": "team"})
        return result

    from mycelium.write_path.direct import write_note_direct

    def _upsert_note(nid: str, t: str, c: str, metadata: dict) -> None:
        notes_collection().upsert(
            ids=[nid],
            documents=[f"{t}\n\n{c}"],
            metadatas=[metadata],
        )

    result = write_note_direct(
        title, content, tags, source_memories, status,
        intent=intent,
        query_notes_fn=_dedupe_query,
        upsert_note_fn=_upsert_note,
        load_note_fn=_vault_load_note,
    )
    log_write("write_note", {**_log_fields, "mode": "personal"})
    return result


@mcp.tool()
def context_federated(
    query: str,
    sources: Optional[list[str]] = None,
    k_per_source: int = 5,
    n_results: int = 20,
) -> str:
    """Federated context across all configured sources.

    Fans out the query in parallel to each registered adapter (mycelium
    notes/drawers/links + any external adapters configured), rank-fuses
    results with per-source bias weights, and returns the top-N.

    Returns JSON with: query, results (each: source, rank, effective_rank,
    id, title, snippet, metadata), source_diagnostics (per-source hits +
    latency), truncated (how many snippets were shortened to fit the cap).

    Args:
        query: natural-language query
        sources: list of adapter names. None or ["all"] = every registered
            adapter. Specific list scopes the fanout
            (e.g. ["mycelium-notes", "gitlab-config"]).
        k_per_source: top-K each source returns BEFORE the merge.
        n_results: final cap on the merged result list.

    Cross-source synthesis is YOUR job — this tool returns ranked candidates,
    not a synthesized answer. The mycelium-context tool is the mycelium-only
    equivalent; use this when you want to pull from external sources too.
    """
    import asyncio
    from mycelium.federation.fanout import federated_context

    result = asyncio.run(federated_context(
        query=query,
        sources_filter=sources,
        k_per_source=k_per_source,
        n_results=n_results,
    ))
    payload = json.dumps(result, indent=2)
    # Response-size observability.
    metrics_dict_size = len(payload.encode("utf-8"))
    from mycelium import metrics as _metrics
    from mycelium.federation.budgets import estimate_tokens
    _metrics.observe("response_size_bytes", float(metrics_dict_size))
    _metrics.observe("response_size_tokens_estimate", float(estimate_tokens(metrics_dict_size)))
    return payload


@mcp.tool()
def delete_note(note_id: str) -> str:
    """Delete a curated note from vault and index.

    Removes both the markdown file and the ChromaDB entry. Prefer this over
    manually deleting the file since it keeps the index in sync.

    Args:
        note_id: The note_xxx ID to delete (from query_notes/context results).
    """
    if not _vault_delete_note(note_id):
        return json.dumps({"error": f"Note not found: {note_id}"})
    try:
        notes_collection().delete(ids=[note_id])
    except Exception:
        pass
    log_write("delete_note", {"note_id": note_id})
    return f"Deleted: {note_id}"


@mcp.tool()
def query_notes(query: str, n_results: int = 10, _caller: str = "agent") -> str:
    """Search curated synthesis notes by semantic similarity.

    Notes are more authoritative than drawers — they represent settled
    decisions and synthesis. Each result includes note_id for link traversal.

    Args:
        query: Natural-language description of what you're looking for.
        n_results: Max notes (default 5).

    Returns:
        JSON with "notes" list. Each note includes note_id for query_links traversal.
    """
    _t0 = time.perf_counter()
    col   = notes_collection()
    count = col.count()
    notes: list[dict] = []

    if count > 0:
        results = col.query(
            query_texts=[query],
            n_results=min(n_results, count),
            include=["documents", "metadatas", "distances"],
        )
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            slug = meta.get("slug") or _slugify(meta.get("title", ""))
            notes.append({
                "note_id":         _note_id_fn(slug),
                "title":           meta.get("title"),
                "tags":            json.loads(meta.get("tags", "[]")),
                "distance":        round(dist, 4),
                "content":         doc,
                "filepath":        meta.get("filepath"),
                "created_at":      meta.get("created_at"),
                "source_memories": json.loads(meta.get("source_memories", "[]")),
            })

    log_retrieval(
        tool="query_notes",
        query=query,
        params={"n_results": n_results},
        notes=notes,
        caller=_caller,
        latency_ms=(time.perf_counter() - _t0) * 1000,
    )

    return json.dumps({"notes": notes, "total_indexed": count}, indent=2)


@mcp.tool()
def context_titles(
    query: str,
    n_notes: int = 60,
    n_drawers: int = 60,
    n_links: int = 20,
    max_distance: float = 0.75,
    _caller: str = "agent",
) -> str:
    """Lightweight context for index-style injection — semantic search
    returns titles + minimal metadata, NO full note/drawer content.

    Designed for the UserPromptSubmit hook: every prompt gets relevant
    titles surfaced as an index, agent fetches full content on demand via
    `context`, `query_notes`, or `get_drawer`. Keeps the per-prompt
    context-injection cost bounded (~1-3 KB) so the hook can run on every
    prompt without bloating the conversation history.

    Args:
        query: Natural-language search query (typically the user's prompt).
        n_notes: Max note titles to return (default 60).
        n_drawers: Max drawer titles/snippets (default 60).
        n_links: Max related links (default 20).
        max_distance: Cosine distance ceiling for drawer results.

    Returns:
        JSON: {
          "query": str,
          "notes":   [{note_id, title, tags, distance, filepath}],
          "drawers": [{drawer_id, wing, room, snippet, distance}],
          "links":   [{link_id, source, target, relation_type, description}]
        }

        Drawer `snippet` is first 100 chars of content (newlines flattened),
        with the drawer id used as a fallback when the body is empty.
    """
    _t0 = time.perf_counter()
    # Fan out the three independent chroma queries concurrently.
    notes_str, drawer_result, links_str = parallel_fanout(
        lambda: query_notes(query, n_notes),
        lambda: search_drawers(query, n_results=n_drawers, max_distance=max_distance),
        lambda: find_links(query, n_links),
    )

    # Notes: strip the full content field.
    notes_lite = [
        {
            "note_id":  n.get("note_id"),
            "title":    n.get("title"),
            "tags":     n.get("tags", []),
            "distance": n.get("distance"),
            "filepath": n.get("filepath"),
        }
        for n in json.loads(notes_str).get("notes", [])
    ]

    # Drawers: snippet only.
    drawers_lite = []
    for d in drawer_result.get("results", []):
        did = d.get("drawer_id", "")
        snippet = (d.get("text") or "").strip().replace("\n", " ")[:100]
        if not snippet:
            # Fall back to the drawer id when body is empty.
            snippet = did
        drawers_lite.append({
            "drawer_id": did,
            "wing":      d.get("wing"),
            "room":      d.get("room"),
            "snippet":   snippet,
            "distance":  d.get("distance"),
        })

    # Links: find_links already returns metadata only.
    links_result = json.loads(links_str)

    log_retrieval(
        tool="context_titles",
        query=query,
        params={
            "n_notes": n_notes,
            "n_drawers": n_drawers,
            "n_links": n_links,
            "max_distance": max_distance,
        },
        notes=notes_lite,
        drawers=drawers_lite,
        links=links_result.get("links", []),
        caller=_caller,
        latency_ms=(time.perf_counter() - _t0) * 1000,
    )

    return json.dumps({
        "query":   query,
        "notes":   notes_lite,
        "drawers": drawers_lite,
        "links":   links_result.get("links", []),
    })


# ---------------------------------------------------------------------------
# Links (typed semantic graph)
# ---------------------------------------------------------------------------

@mcp.tool()
def add_link(
    source_label: str,
    source_type: str,
    source_id: str,
    target_label: str,
    target_type: str,
    target_id: str,
    relation_type: str,
    description: str,
    ended_at: str = "",
) -> str:
    """Add a typed semantic link between two entities in the knowledge graph.

    Use to capture relationships between notes, drawers, or free-form concepts.
    Call whenever you notice entities that should be connected.

    Recommended relation types (any string is valid):
      uses, part_of, extends, contradicts, causes, implements,
      depends_on, hosted_on, configured_by, related_to

    Args:
        source_label: Human-readable name of the source.
        source_type: "note" | "drawer" | "concept"
        source_id: Stable ID — note_xxx, drawer_xxx, or label for concepts.
        target_label: Human-readable name of the target.
        target_type: "note" | "drawer" | "concept"
        target_id: Stable ID of the target.
        relation_type: The typed relationship.
        description: Natural language sentence describing the relationship.
        ended_at: ISO timestamp when this relationship ended. Empty = active.
            To mark a link historical: re-call with ended_at set to end timestamp.

    Returns:
        Confirmation with link_id.
    """
    # Disk-first: write the link to vault/links/{id}.md (idempotent upsert)
    lid, filepath = _vault_write_link(
        source_id, source_type, source_label,
        target_id, target_type, target_label,
        relation_type, description, ended_at,
    )

    # Read back the persisted created_at (preserved across re-upserts)
    from mycelium.vault import load_link
    persisted = load_link(filepath) or {}
    created_at = persisted.get("created_at") or datetime.now(timezone.utc).isoformat()

    # Index in chroma (derived from disk)
    doc = f"{source_label} {relation_type} {target_label}. {description}"
    links_collection().upsert(
        ids=[lid],
        documents=[doc],
        metadatas=[{
            "source_id":     source_id,
            "source_type":   source_type,
            "source_label":  source_label,
            "target_id":     target_id,
            "target_type":   target_type,
            "target_label":  target_label,
            "relation_type": relation_type,
            "description":   description,
            "created_at":    created_at,
            "ended_at":      ended_at,
        }],
    )
    log_write("add_link", {
        "link_id": lid,
        "source_id": source_id,
        "source_type": source_type,
        "target_id": target_id,
        "target_type": target_type,
        "relation_type": relation_type,
        "description_length": len(description or ""),
        "is_historical": bool(ended_at),
    })
    return f"Link added: {lid}\n{source_label} --[{relation_type}]--> {target_label}"


@mcp.tool()
def query_links(
    entity_id: str,
    direction: str = "both",
    include_historical: bool = False,
) -> str:
    """Traverse the knowledge graph: get all links for a given entity.

    Args:
        entity_id: The ID to query (note_xxx, drawer_xxx, or concept label).
        direction: "outgoing" | "incoming" | "both" (default).
        include_historical: Include links with ended_at set (default False).

    Returns:
        JSON with "outgoing" and/or "incoming" link arrays.
    """
    col = links_collection()
    if col.count() == 0:
        return json.dumps({"entity_id": entity_id, "outgoing": [], "incoming": []})

    result: dict = {"entity_id": entity_id}

    if direction in ("outgoing", "both"):
        where_out = (
            {"source_id": entity_id}
            if include_historical
            else {"$and": [{"source_id": entity_id}, {"ended_at": ""}]}
        )
        out = col.get(where=where_out, include=["metadatas"])
        result["outgoing"] = _format_links(out)

    if direction in ("incoming", "both"):
        where_inc = (
            {"target_id": entity_id}
            if include_historical
            else {"$and": [{"target_id": entity_id}, {"ended_at": ""}]}
        )
        inc = col.get(where=where_inc, include=["metadatas"])
        result["incoming"] = _format_links(inc)

    return json.dumps(result, indent=2)


@mcp.tool()
def find_links(
    query: str,
    n_results: int = 10,
    include_historical: bool = False,
    _caller: str = "agent",
) -> str:
    """Search the knowledge graph by semantic similarity over link descriptions.

    Args:
        query: Natural-language description of the relationship you're looking for.
        n_results: Max links (default 10).
        include_historical: Include links with ended_at set (default False).

    Returns:
        JSON with "links" list.
    """
    _t0 = time.perf_counter()
    col   = links_collection()
    count = col.count()
    links: list[dict] = []

    if count > 0:
        where_filter = None if include_historical else {"ended_at": ""}
        results = col.query(
            query_texts=[query],
            n_results=min(n_results, count),
            where=where_filter,
            include=["metadatas", "distances"],
        )
        for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
            links.append({
                "link_id":       _link_id_fn(meta["source_id"], meta["relation_type"], meta["target_id"]),
                "source":        {"id": meta["source_id"], "label": meta["source_label"], "type": meta["source_type"]},
                "relation_type": meta["relation_type"],
                "target":        {"id": meta["target_id"], "label": meta["target_label"], "type": meta["target_type"]},
                "description":   meta["description"],
                "distance":      round(dist, 4),
                "ended_at":      meta.get("ended_at", "") or None,
            })

    log_retrieval(
        tool="find_links",
        query=query,
        params={"n_results": n_results, "include_historical": include_historical},
        links=links,
        caller=_caller,
        latency_ms=(time.perf_counter() - _t0) * 1000,
    )

    return json.dumps({"links": links, "total_indexed": count}, indent=2)


@mcp.tool()
def delete_link(link_id: str) -> str:
    """Delete a link from the knowledge graph (disk + index).

    Prefer marking links historical with ended_at over deleting them,
    to preserve the record of past relationships.

    Args:
        link_id: The link_xxx ID to delete.
    """
    _vault_delete_link_file(link_id)
    try:
        links_collection().delete(ids=[link_id])
    except Exception:
        pass
    log_write("delete_link", {"link_id": link_id})
    return f"Deleted: {link_id}"


# ---------------------------------------------------------------------------
# Diary
# ---------------------------------------------------------------------------

@mcp.tool()
def diary_write(content: str, session_id: str = "") -> str:
    """Append an entry to today's diary.

    Use at stop checkpoints for AAAK-compressed session summaries.
    Format: SESSION:date|TOPIC:...|key.facts|DECISIONS:...|OPEN:...

    Args:
        content: Entry content.
        session_id: Optional session ID for attribution.

    In team mode (MYCELIUM_DEPLOYMENT_MODE=team), the daily file is written
    synchronously and a diary signal is enqueued; the worker re-indexes the
    day's full file into chroma asynchronously.
    """
    from mycelium.config import DEPLOYMENT_MODE

    today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _log_fields = {
        "date": today_iso,
        "session_id": session_id or None,
        "size_bytes": len(content.encode("utf-8")),
    }

    if DEPLOYMENT_MODE == "team":
        from mycelium.write_path.queued import diary_write_queued
        result = diary_write_queued(content, session_id)
        log_write("diary_write", {**_log_fields, "mode": "team"})
        return result

    filepath = _vault_diary_write(content, session_id)
    log_write("diary_write", {**_log_fields, "mode": "personal"})
    return f"Diary entry written: {filepath}"


@mcp.tool()
def diary_read(date: Optional[str] = None, n_days: int = 3) -> str:
    """Read diary entries.

    Args:
        date: Specific date in YYYY-MM-DD format, or None for recent entries.
        n_days: How many recent days to include when date is None (default 3).
    """
    return _vault_diary_read(date=date, n_days=n_days)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

AAAK_SPEC = """AAAK is a compressed memory dialect used for diary entries and dense
summaries. Designed to be readable by both humans and LLMs without decoding.

FORMAT:
  ENTITIES   3-letter uppercase codes per person/system (ALC=Alice, JOR=Jordan).
  EMOTIONS   *action markers* before/in text: *warm*=joy, *fierce*=determined,
             *raw*=vulnerable, *bloom*=tenderness.
  STRUCTURE  Pipe-separated fields: FAM: family | PROJ: projects | WARN: warnings.
  DATES      ISO format (2026-05-20).
  COUNTS     Nx = N mentions (e.g. 570x).
  IMPORTANCE Star scale ★ to ★★★★★.
  WINGS      Domain-scoped: wing_user, wing_agent, wing_code, wing_claude, ...
  ROOMS      Hyphenated slugs naming an aspect: chromadb-setup, gpu-pricing.

EXAMPLE
  SESSION:2026-05-20|TOPIC:mycelium.rebuild.phase1|★★★★★
  FIXED: contextforge.tools.refresh.endpoint.discovered → manual.DB.insert.avoided
  KEY.DECISION: disk-first.vault.canonical, chromadb.derived.only

When WRITING AAAK: use entity codes, mark emotions, compress structure.
When READING AAAK: expand codes mentally, *markers* are emotional context.
"""


@mcp.tool()
def get_aaak_spec() -> str:
    """Return the AAAK dialect specification — the compressed notation used
    in diary entries and dense session summaries. Call once at session start
    if writing AAAK content (e.g. via diary_write).
    """
    return AAAK_SPEC


@mcp.tool()
def get_taxonomy() -> str:
    """Return the full wing → room → count tree of indexed drawers.

    Useful before filing (which wings/rooms exist?) or to get a one-shot
    overview of what's in the vault. Reads from the drawers ChromaDB
    collection so it reflects what search would find.

    Counts are deduplicated by canonical drawer_id (multi-chunk drawers
    with IDs like `{drawer_id}__c{N}` count once).

    Returns:
        JSON with structure {"taxonomy": {wing: {room: count, ...}, ...}}.
    """
    col          = drawers_collection()
    total_chunks = col.count()
    if total_chunks == 0:
        return json.dumps({"taxonomy": {}, "total": 0})

    taxonomy: dict[str, dict[str, int]] = {}
    seen_drawers: set[str] = set()

    # Chunked pagination — ChromaDB's underlying SQLite has a 999-param
    # default limit on IN-clauses, so an unbounded col.get() blows up
    # past ~1k items. 500 keeps margin for the other bound params.
    batch  = 500
    offset = 0
    while offset < total_chunks:
        page  = col.get(include=["metadatas"], limit=batch, offset=offset)
        ids   = page.get("ids") or []
        metas = page.get("metadatas") or []
        for did, m in zip(ids, metas):
            # Strip __cN chunk suffix to get the canonical drawer ID.
            canonical = did.split("__c", 1)[0]
            if canonical in seen_drawers:
                continue
            seen_drawers.add(canonical)
            m = m or {}
            w = m.get("wing", "unknown")
            r = m.get("room", "unknown")
            taxonomy.setdefault(w, {})
            taxonomy[w][r] = taxonomy[w].get(r, 0) + 1
        offset += batch

    # Sort wings and rooms for stable output
    taxonomy_sorted = {
        w: dict(sorted(rooms.items())) for w, rooms in sorted(taxonomy.items())
    }
    return json.dumps({"taxonomy": taxonomy_sorted, "total": len(seen_drawers)}, indent=2)


@mcp.tool()
def get_room_drawers(wing: str, room: str, limit: int = 500) -> str:
    """List the drawers in a given wing/room, with content snippets.

    Powers the drawer-level drill-down on the Memory Taxonomy graph.
    Returns canonical drawer IDs (chunked drawers count once) plus the
    first ~120 chars of content as a snippet so the UI can label nodes
    without fetching each drawer separately.

    Args:
        wing: Wing name to filter by (e.g. "mycelium").
        room: Room name within the wing (e.g. "decisions").
        limit: Max drawers to return (default 500). The graph rendering
            tops out around this size; for huge rooms callers should
            use the count from get_taxonomy and offer a search fallback.

    Returns:
        JSON with structure:
        {
            "wing": str, "room": str, "total": int (real total, not limited),
            "returned": int, "truncated": bool,
            "drawers": [{"id": str, "snippet": str}, ...]
        }
    """
    col = drawers_collection()
    batch = 500
    offset = 0
    seen: set[str] = set()
    matches: list[dict] = []
    total_in_room = 0

    # Need to also include documents for the snippets, but chroma returns
    # everything by ID order — we still iterate the whole collection
    # because there's no native (wing, room) index. With the new chunked
    # pagination this is fine even on the 97k vault.
    while True:
        page = col.get(
            include=["metadatas", "documents"],
            limit=batch,
            offset=offset,
        )
        ids = page.get("ids") or []
        if not ids:
            break
        metas = page.get("metadatas") or []
        docs  = page.get("documents") or []
        for did, m, doc in zip(ids, metas, docs):
            canonical = did.split("__c", 1)[0]
            if canonical in seen:
                continue
            m = m or {}
            if m.get("wing") != wing or m.get("room") != room:
                continue
            seen.add(canonical)
            total_in_room += 1
            if len(matches) < limit:
                snippet = (doc or "").strip().replace("\n", " ")[:120]
                matches.append({"id": canonical, "snippet": snippet})
        offset += batch

    return json.dumps({
        "wing":      wing,
        "room":      room,
        "total":     total_in_room,
        "returned":  len(matches),
        "truncated": total_in_room > len(matches),
        "drawers":   matches,
    })


@mcp.tool()
def status() -> str:
    """Report vault and index counts.

    Returns:
        JSON with note_count, drawer_count, link_count, diary_days.
    """
    from mycelium.vault import DRAWERS_DIR, DIARY_DIR, NOTES_DIR

    note_count   = len(list(NOTES_DIR.glob("*.md")))   if NOTES_DIR.exists()   else 0
    drawer_count = len(list(DRAWERS_DIR.glob("*.md"))) if DRAWERS_DIR.exists() else 0
    diary_days   = len(list(DIARY_DIR.glob("*.md")))   if DIARY_DIR.exists()   else 0

    indexed_notes   = notes_collection().count()
    indexed_drawers = drawers_collection().count()
    indexed_links   = links_collection().count()

    return json.dumps({
        "vault": {
            "notes":      note_count,
            "drawers":    drawer_count,
            "diary_days": diary_days,
        },
        "index": {
            "notes":   indexed_notes,
            "drawers": indexed_drawers,
            "links":   indexed_links,
        },
    }, indent=2)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _index_drawer(did: str, content: str, wing: str, room: str, filepath: str) -> None:
    """Index a drawer's content into chroma, chunking if large.

    When the drawer is being re-indexed (update), we first remove all existing
    chunks for this drawer_id so chunk count changes don't leave orphans.
    """
    col = drawers_collection()
    # Clean any prior chunks for this drawer_id (so an update with different
    # chunk count doesn't leave stale chunks indexed)
    existing = col.get(where={"drawer_id": did}, include=[])["ids"]
    if existing:
        col.delete(ids=existing)

    chunks = _chunk_content(content)
    total_chunks = len(chunks)
    now = datetime.now(timezone.utc).isoformat()
    ids, docs, metas = [], [], []
    for i, chunk_text in enumerate(chunks):
        chunk_id = did if total_chunks == 1 else f"{did}__c{i}"
        ids.append(chunk_id)
        docs.append(chunk_text)
        metas.append({
            "drawer_id":    did,
            "chunk_index":  i,
            "total_chunks": total_chunks,
            "wing":         wing,
            "room":         room,
            "filed_at":     now,
            "source_file":  filepath,
        })
    col.upsert(ids=ids, documents=docs, metadatas=metas)


def _index_drawer_from_disk(did: str) -> None:
    d = _vault_get_drawer(did)
    if d:
        _index_drawer(d["drawer_id"], d["content"], d["wing"], d["room"], d["filepath"])


def _format_links(chroma_result: dict) -> list[dict]:
    links = []
    for meta in chroma_result.get("metadatas", []):
        links.append({
            "link_id":       _link_id_fn(meta["source_id"], meta["relation_type"], meta["target_id"]),
            "source":        {"id": meta["source_id"], "label": meta["source_label"], "type": meta["source_type"]},
            "relation_type": meta["relation_type"],
            "target":        {"id": meta["target_id"], "label": meta["target_label"], "type": meta["target_type"]},
            "description":   meta["description"],
            "ended_at":      meta.get("ended_at", "") or None,
        })
    return links


# ---------------------------------------------------------------------------
# Skills as MCP resources
# ---------------------------------------------------------------------------
# Each top-level subdirectory of `mycelium/skills/` is published as a static
# MCP resource at `mycelium-skill://<slug>`. The body of the resource is the
# skill's SKILL.md contents. ContextForge (and any other gateway that
# aggregates this server) forwards these resources to downstream clients via
# `resources/list` and `resources/read` by default.
#
# When the bucket is empty (default state) this loop registers nothing — the
# resources capability is still advertised by FastMCP and skills surface
# automatically the moment any are added to the package.

_SKILLS_PACKAGE_DIR  = Path(__file__).parent / "skills"
_SKILLS_PERSONAL_DIR = Path.home() / ".mycelium" / "skills" / "personal"


def _register_skill_resource(slug: str, src_dir: Path, uri_scheme: str, label_prefix: str) -> None:
    """Register a single skill as a static MCP resource.

    Factored to get its own enclosing scope per call — avoids the
    loop-variable closure capture pitfall, and the inner reader takes
    no parameters so FastMCP treats the URI as static (not a template,
    which would require a `{param}` in the URI).
    """

    @mcp.resource(
        f"{uri_scheme}://{slug}",
        name=slug,
        description=f"{label_prefix}: {slug}",
        mime_type="text/markdown",
    )
    def _read_skill() -> str:
        return (src_dir / slug / "SKILL.md").read_text()


def _register_skills_from_dir(src_dir: Path, uri_scheme: str, label_prefix: str) -> None:
    if not src_dir.is_dir():
        return
    for entry in sorted(src_dir.iterdir()):
        if entry.name == "README.md" or not entry.is_dir() or entry.name == ".git":
            continue
        if not (entry / "SKILL.md").exists():
            continue
        _register_skill_resource(entry.name, src_dir, uri_scheme, label_prefix)


# In-package skills (ship with the wheel) → mycelium-skill://<slug>
_register_skills_from_dir(_SKILLS_PACKAGE_DIR, "mycelium-skill", "Mycelium skill")

# Personal-skills repo (synced via `mycelium skills sync`) → personal-skill://<slug>
_register_skills_from_dir(_SKILLS_PERSONAL_DIR, "personal-skill", "Personal skill")
