"""Daily health snapshot — append one aggregated JSONL record to health.jsonl.

Run via cron / systemd timer on the mycelium host:

    python -m mycelium.health_snapshot

Each invocation captures:
- Vault counts: notes, drawers, links, diary entries, concept IDs.
- Wing distribution of drawers (and notes if wing inference applies).
- ChromaDB collection sizes per collection.
- Linkage density: links / notes, links / drawers.
- Note staleness histogram (7/14/30/60/90 days since last update).
- Note status distribution (open/in-progress/done/wont-fix/blocked/unset).
- Deltas vs the previous snapshot (peek at the last line of health.jsonl).
- Log file sizes (retrieval.jsonl, writes.jsonl).

All values are scalars — easy to graph in Grafana or jq one-liners.

Cheap by design: only walks the disk + queries chroma .count(); no
expensive embedding work, no full-table loads beyond reading note
frontmatter (which is small).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from mycelium.chroma import drawers_collection, links_collection, notes_collection
from mycelium.config import (
    DIARY_DIR,
    DRAWERS_DIR,
    HEALTH_LOG_PATH,
    LINKS_DIR,
    NOTES_DIR,
    RETRIEVAL_LOG_PATH,
    WRITE_LOG_PATH,
)

_logger = logging.getLogger(__name__)

# Days back to bucket note staleness.
_STALENESS_BUCKETS = (7, 14, 30, 60, 90)


def _safe_count(coll_fn) -> int:
    try:
        return coll_fn().count()
    except Exception as e:
        _logger.warning("count failed: %s", e)
        return -1


def _file_size(path_str: str | None) -> int:
    if not path_str:
        return 0
    try:
        return Path(path_str).stat().st_size
    except (FileNotFoundError, OSError):
        return 0


def _drawer_taxonomy() -> tuple[dict[str, int], int]:
    """Pull (wing→count, total_unique_drawers) from chroma metadata.

    Mirrors server.get_taxonomy: chunked drawers (`drawer_xxx__c0/__c1/...`)
    are deduped to the canonical drawer_id. Authoritative because drawers
    are stored flat on disk with wing in frontmatter — chroma metadata is
    the cheapest source for the wing histogram.
    """
    try:
        col = drawers_collection()
        total_chunks = col.count()
        if total_chunks == 0:
            return {}, 0

        counts: dict[str, int] = {}
        seen: set[str] = set()
        batch = 500
        offset = 0
        while offset < total_chunks:
            page = col.get(include=["metadatas"], limit=batch, offset=offset)
            ids = page.get("ids") or []
            metas = page.get("metadatas") or []
            for did, m in zip(ids, metas):
                canonical = did.split("__c", 1)[0]
                if canonical in seen:
                    continue
                seen.add(canonical)
                wing = (m or {}).get("wing", "unknown")
                counts[wing] = counts.get(wing, 0) + 1
            offset += batch
        return counts, len(seen)
    except Exception as e:
        _logger.warning("drawer taxonomy failed: %s", e)
        return {}, -1


def _note_staleness_and_status() -> tuple[dict[str, int], dict[str, int]]:
    """Walk vault/notes/*.md, parse frontmatter, bucket by age + status.

    Age uses `updated_at` if present, else `created`.
    """
    staleness = {f"_le_{n}d": 0 for n in _STALENESS_BUCKETS}
    staleness["_gt_max"] = 0  # older than the largest bucket
    staleness["_undated"] = 0

    status_counts: dict[str, int] = {}
    now = datetime.now(timezone.utc)

    if not NOTES_DIR.exists():
        return staleness, status_counts

    for f in NOTES_DIR.glob("*.md"):
        try:
            post = frontmatter.load(f)
        except Exception:
            continue

        status = post.metadata.get("status") or "_unset"
        status_counts[status] = status_counts.get(status, 0) + 1

        raw_ts = post.metadata.get("updated_at") or post.metadata.get("created")
        if not raw_ts:
            staleness["_undated"] += 1
            continue
        try:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            staleness["_undated"] += 1
            continue

        age_days = (now - ts).days
        placed = False
        for bucket in _STALENESS_BUCKETS:
            if age_days <= bucket:
                staleness[f"_le_{bucket}d"] += 1
                placed = True
                break
        if not placed:
            staleness["_gt_max"] += 1

    return staleness, status_counts


def _concept_count() -> int:
    """Count unique entity IDs in links that aren't notes or drawers.

    A "concept" in mycelium is an untyped entity that exists only as a
    link source or target. Reading from chroma is cheapest; we pull
    all link metadatas and tally unique non-note/non-drawer IDs.
    """
    try:
        col = links_collection()
        n = col.count()
        if n == 0:
            return 0
        all_meta = col.get(include=["metadatas"]).get("metadatas", [])
    except Exception as e:
        _logger.warning("concept count failed: %s", e)
        return -1

    concept_ids: set[str] = set()
    for meta in all_meta:
        for side in ("source", "target"):
            t = meta.get(f"{side}_type")
            if t == "concept":
                concept_ids.add(str(meta.get(f"{side}_id") or ""))
    concept_ids.discard("")
    return len(concept_ids)


def _diary_count() -> int:
    if not DIARY_DIR.exists():
        return 0
    return sum(1 for f in DIARY_DIR.glob("*.md"))


def _link_files_count() -> int:
    if not LINKS_DIR.exists():
        return 0
    return sum(1 for f in LINKS_DIR.glob("*.md"))


def _note_files_count() -> int:
    if not NOTES_DIR.exists():
        return 0
    return sum(1 for f in NOTES_DIR.glob("*.md"))


def _last_snapshot() -> dict[str, Any] | None:
    """Read the last JSONL line from health.jsonl for delta computation."""
    if not HEALTH_LOG_PATH:
        return None
    path = Path(HEALTH_LOG_PATH)
    if not path.exists():
        return None
    try:
        # Read last line (file is small, append-only — straight read is fine).
        last = None
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if last:
            return json.loads(last)
    except Exception as e:
        _logger.warning("could not read previous snapshot: %s", e)
    return None


def _delta(curr: dict[str, Any], prev: dict[str, Any] | None) -> dict[str, Any]:
    if not prev:
        return {}
    counts_keys = ("notes", "drawers", "links", "diary_entries", "concepts")
    return {
        k: curr["counts"][k] - prev.get("counts", {}).get(k, 0)
        for k in counts_keys
        if isinstance(curr["counts"].get(k), int) and isinstance(prev.get("counts", {}).get(k), int)
    }


def collect_snapshot() -> dict[str, Any]:
    """Compute one health snapshot record. Pure — does not write."""
    notes_indexed   = _safe_count(notes_collection)
    drawers_indexed = _safe_count(drawers_collection)
    links_indexed   = _safe_count(links_collection)

    n_notes   = _note_files_count()
    wing_dist, n_drawers_canonical = _drawer_taxonomy()
    n_links   = _link_files_count()
    n_diary   = _diary_count()
    n_concepts = _concept_count()
    staleness, status_dist = _note_staleness_and_status()

    snapshot: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "notes": n_notes,
            "drawers": n_drawers_canonical,
            "links": n_links,
            "diary_entries": n_diary,
            "concepts": n_concepts,
        },
        "chroma_indexed": {
            "notes": notes_indexed,
            "drawers": drawers_indexed,
            "links": links_indexed,
        },
        "wing_distribution_drawers": wing_dist,
        "note_staleness": staleness,
        "note_status_distribution": status_dist,
        "linkage_density": {
            "links_per_note": round(n_links / n_notes, 3) if n_notes else 0.0,
            "links_per_drawer": round(n_links / n_drawers_canonical, 5) if n_drawers_canonical else 0.0,
        },
        "log_sizes_bytes": {
            "retrieval_log": _file_size(RETRIEVAL_LOG_PATH),
            "write_log": _file_size(WRITE_LOG_PATH),
            "health_log": _file_size(HEALTH_LOG_PATH),
        },
    }
    snapshot["deltas_vs_previous"] = _delta(snapshot, _last_snapshot())
    return snapshot


def append_snapshot(snapshot: dict[str, Any]) -> Path | None:
    if not HEALTH_LOG_PATH:
        return None
    path = Path(HEALTH_LOG_PATH)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snapshot, separators=(",", ":"), ensure_ascii=False) + "\n")
        return path
    except Exception as e:
        _logger.error("health snapshot write failed: %s", e)
        return None


def main() -> int:
    snapshot = collect_snapshot()
    path = append_snapshot(snapshot)
    # Echo a brief one-liner to stdout for cron mail / log aggregator.
    print(
        f"mycelium health snapshot: notes={snapshot['counts']['notes']} "
        f"drawers={snapshot['counts']['drawers']} "
        f"links={snapshot['counts']['links']} "
        f"diary={snapshot['counts']['diary_entries']} "
        f"concepts={snapshot['counts']['concepts']} "
        f"-> {path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
