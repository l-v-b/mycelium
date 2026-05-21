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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import frontmatter
from fastmcp import FastMCP

from mycelium.chroma import drawers_collection, links_collection, notes_collection
from mycelium.config import NOTES_DIR, VAULT_DIR
from mycelium.search import search_drawers
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
        "Mycelium is a unified memory system: verbatim captures (drawers) + curated notes + typed links. "
        "Start every session with context(query) to pull relevant knowledge. "
        "Use file() to capture verbatim content; write_note() for settled conclusions. "
        "Use add_link() whenever you notice connected entities that lack a link. "
        "Drawers are the source of truth for raw content; notes are authoritative for decisions."
    ),
)


# ---------------------------------------------------------------------------
# Context (primary entry point)
# ---------------------------------------------------------------------------

@mcp.tool()
def context(
    query: str,
    n_notes: int = 10,
    n_drawers: int = 10,
    n_links: int = 10,
    max_distance: float = 0.75,
    expand_links: bool = True,
) -> str:
    """Retrieve combined context: curated notes + verbatim drawers + related links.

    The primary tool for task start. Returns all three layers in one call, plus
    optionally the graph neighborhood — outgoing links from any returned entity
    so the agent sees connected content without a second tool call.

    Args:
        query: What you are looking for.
        n_notes: Max curated notes (default 10).
        n_drawers: Max verbatim drawers (default 10).
        n_links: Max links found via semantic search on link descriptions (default 10).
        max_distance: Cosine distance ceiling for drawer results (default 0.75).
        expand_links: If True (default), also include outgoing links from each
            returned note/drawer under "links_from_results". Provides the
            graph neighborhood of the matched entities.

    Returns:
        JSON with notes, memories (drawers), links (semantic on descriptions),
        and (if expand_links) links_from_results (graph edges from matches).
    """
    notes_result  = json.loads(query_notes(query, n_notes))
    notes         = notes_result.get("notes", [])

    drawer_result = search_drawers(query, n_results=n_drawers, max_distance=max_distance)
    drawers       = drawer_result.get("results", [])

    links_result  = json.loads(find_links(query, n_links))
    links         = links_result.get("links", [])

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
    return json.dumps(
        search_drawers(query, wing=wing, room=room, n_results=n_results, max_distance=max_distance),
        indent=2,
    )


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
    """
    did, filepath = _vault_write_drawer(content, wing, room)
    _index_drawer(did, content, wing, room, str(filepath))
    try:
        _vault_update_closet_for_drawer(did, wing, room, content)
    except Exception:
        pass  # closet update is best-effort, never blocks filing
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
        tags: Topic tags (e.g. ["infra", "decision"]).
        source_memories: Drawer IDs this note was derived from.
        status: Optional. Set when this note represents tracked work.
            Canonical values: "open" | "in-progress" | "done" | "wont-fix" | "blocked".
            Leave unset (None) for synthesis notes that aren't work items, or
            on upsert to preserve the existing status. Pass "" to clear.

    Returns:
        Confirmation with note_id and file path.
    """
    tags = tags or []
    source_memories = source_memories or []

    nid, filepath = _vault_write_note(title, content, tags, source_memories, status)
    now = datetime.now(timezone.utc).isoformat()

    loaded = _vault_load_note(filepath)
    final_status = loaded.get("status", "") if loaded else (status or "")

    metadata = {
        "title":           title,
        "slug":            _slugify(title),
        "tags":            json.dumps(tags),
        "source_memories": json.dumps(source_memories),
        "created_at":      now,
        "filepath":        str(filepath),
    }
    if final_status:
        metadata["status"] = final_status

    notes_collection().upsert(
        ids=[nid],
        documents=[f"{title}\n\n{content}"],
        metadatas=[metadata],
    )
    return f"Note written: {nid}\nFile: {filepath}"


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
    return f"Deleted: {note_id}"


@mcp.tool()
def query_notes(query: str, n_results: int = 10) -> str:
    """Search curated synthesis notes by semantic similarity.

    Notes are more authoritative than drawers — they represent settled
    decisions and synthesis. Each result includes note_id for link traversal.

    Args:
        query: Natural-language description of what you're looking for.
        n_results: Max notes (default 5).

    Returns:
        JSON with "notes" list. Each note includes note_id for query_links traversal.
    """
    col   = notes_collection()
    count = col.count()
    if count == 0:
        return json.dumps({"notes": [], "total_indexed": 0})

    results = col.query(
        query_texts=[query],
        n_results=min(n_results, count),
        include=["documents", "metadatas", "distances"],
    )

    notes = []
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

    return json.dumps({"notes": notes, "total_indexed": count}, indent=2)


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
) -> str:
    """Search the knowledge graph by semantic similarity over link descriptions.

    Args:
        query: Natural-language description of the relationship you're looking for.
        n_results: Max links (default 10).
        include_historical: Include links with ended_at set (default False).

    Returns:
        JSON with "links" list.
    """
    col   = links_collection()
    count = col.count()
    if count == 0:
        return json.dumps({"links": [], "total_indexed": 0})

    where_filter = None if include_historical else {"ended_at": ""}
    results = col.query(
        query_texts=[query],
        n_results=min(n_results, count),
        where=where_filter,
        include=["metadatas", "distances"],
    )

    links = []
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
    """
    filepath = _vault_diary_write(content, session_id)
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

    Returns:
        JSON with structure {"taxonomy": {wing: {room: count, ...}, ...}}.
    """
    col   = drawers_collection()
    count = col.count()
    if count == 0:
        return json.dumps({"taxonomy": {}, "total": 0})

    all_meta = col.get(include=["metadatas"])["metadatas"]
    taxonomy: dict[str, dict[str, int]] = {}
    for m in all_meta:
        m = m or {}
        w = m.get("wing", "unknown")
        r = m.get("room", "unknown")
        taxonomy.setdefault(w, {})
        taxonomy[w][r] = taxonomy[w].get(r, 0) + 1
    # Sort wings and rooms for stable output
    taxonomy_sorted = {
        w: dict(sorted(rooms.items())) for w, rooms in sorted(taxonomy.items())
    }
    return json.dumps({"taxonomy": taxonomy_sorted, "total": count}, indent=2)


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
