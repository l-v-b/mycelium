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
- Vault git sync: unpushed commit count + last push outcome.
- Sub-task adoption: how many tracked notes actually use `- [ ]` checkboxes.
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
import subprocess
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
from mycelium.vault import (
    UNFINISHED_STATUSES,
    _GIT_LOG,
    _git_args,
    parse_subtasks,
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


def _note_metrics() -> tuple[dict[str, int], dict[str, int], dict[str, Any]]:
    """Walk vault/notes/*.md once: bucket by age + status, and tally sub-tasks.

    Age uses `updated_at` if present, else `created`.

    Sub-task adoption is tracked because the todo convention has two levels —
    frontmatter `status` for the note, `- [ ]` checkboxes for the breakdown —
    and as of 2026-07 only 18 of 210 tracked notes used the second one. Whether
    that ratio moves decides how much granularity any generated MOC can show,
    so it is measured over time rather than re-counted by hand.
    """
    staleness = {f"_le_{n}d": 0 for n in _STALENESS_BUCKETS}
    staleness["_gt_max"] = 0  # older than the largest bucket
    staleness["_undated"] = 0

    status_counts: dict[str, int] = {}
    subs = {
        "tracked_notes": 0,
        "tracked_with_subtasks": 0,
        "unfinished_notes": 0,
        "unfinished_with_subtasks": 0,
        "subtasks_total": 0,
        "subtasks_done": 0,
    }
    now = datetime.now(timezone.utc)

    if not NOTES_DIR.exists():
        return staleness, status_counts, _finalise_subtasks(subs)

    for f in NOTES_DIR.glob("*.md"):
        try:
            post = frontmatter.load(f)
        except Exception:
            continue

        raw_status = post.metadata.get("status")
        status = raw_status or "_unset"
        status_counts[status] = status_counts.get(status, 0) + 1

        if raw_status:
            norm = str(raw_status).strip().lower()
            subtasks = parse_subtasks(post.content or "")
            subs["tracked_notes"] += 1
            subs["subtasks_total"] += len(subtasks)
            subs["subtasks_done"] += sum(1 for s in subtasks if s["done"])
            if subtasks:
                subs["tracked_with_subtasks"] += 1
            if norm in UNFINISHED_STATUSES:
                subs["unfinished_notes"] += 1
                if subtasks:
                    subs["unfinished_with_subtasks"] += 1

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

    return staleness, status_counts, _finalise_subtasks(subs)


def _finalise_subtasks(subs: dict[str, int]) -> dict[str, Any]:
    """Add derived percentages so the adoption trend is graphable directly."""
    out: dict[str, Any] = dict(subs)
    out["adoption_pct"] = (
        round(100 * subs["tracked_with_subtasks"] / subs["tracked_notes"], 1)
        if subs["tracked_notes"] else 0.0
    )
    out["unfinished_adoption_pct"] = (
        round(100 * subs["unfinished_with_subtasks"] / subs["unfinished_notes"], 1)
        if subs["unfinished_notes"] else 0.0
    )
    out["subtasks_open"] = subs["subtasks_total"] - subs["subtasks_done"]
    return out


def _vault_git_sync() -> dict[str, Any]:
    """Unpushed-commit count + last push outcome for the vault repo.

    Auto-push has failed silently three separate times — never implemented,
    then no ssh client in the image, then an unreadable ~/.ssh/config — and
    every time it was found by accident days later, because the local commit
    is the visible half and the push is the invisible half. `unpushed_commits`
    is the single number that would have caught all three the next morning.

    No network call: a successful `git push` fast-forwards the local
    `origin/*` ref, so comparing HEAD against it is accurate without a fetch
    (and a fetch here would make the daily snapshot depend on connectivity).
    """
    out: dict[str, Any] = {
        "unpushed_commits": -1,
        "upstream": "",
        "last_push_failed": None,
        "last_push_error": "",
    }
    try:
        up = subprocess.run(
            _git_args() + ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"],
            capture_output=True, timeout=15,
        )
        if up.returncode != 0:
            out["last_push_error"] = "no upstream configured"
            return out
        upstream = up.stdout.decode("utf-8", "replace").strip()
        out["upstream"] = upstream

        cnt = subprocess.run(
            _git_args() + ["rev-list", "--count", f"{upstream}..HEAD"],
            capture_output=True, timeout=15,
        )
        if cnt.returncode == 0:
            out["unpushed_commits"] = int(cnt.stdout.decode("utf-8", "replace").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        out["last_push_error"] = f"git query failed: {e}"
        return out

    # Tail the async-push log for the outcome of the most recent attempt.
    # NOTE: this log lives inside the container and is wiped by a rebuild, so
    # treat a missing log as unknown, never as success.
    try:
        if _GIT_LOG.exists():
            tail = _GIT_LOG.read_text(encoding="utf-8", errors="replace")[-4000:]
            _, _, latest = tail.rpartition("push initiated")
            if latest:
                lowered = latest.lower()
                failed = any(m in lowered for m in ("fatal:", "error:", "bad owner", "permission denied"))
                out["last_push_failed"] = failed
                if failed:
                    out["last_push_error"] = " ".join(latest.split())[:300]
    except OSError:
        pass

    return out


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
    staleness, status_dist, subtask_adoption = _note_metrics()
    git_sync = _vault_git_sync()

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
        "subtask_adoption": subtask_adoption,
        "vault_git_sync": git_sync,
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
    git = snapshot["vault_git_sync"]
    print(
        f"mycelium health snapshot: notes={snapshot['counts']['notes']} "
        f"drawers={snapshot['counts']['drawers']} "
        f"links={snapshot['counts']['links']} "
        f"diary={snapshot['counts']['diary_entries']} "
        f"concepts={snapshot['counts']['concepts']} "
        f"unpushed={git['unpushed_commits']} "
        f"-> {path}"
    )
    # Loud on stderr so cron mails it: unpushed commits mean the off-host
    # backup has silently stopped, which has happened three times and was
    # never noticed from the normal output.
    if git["unpushed_commits"] > 0 or git["last_push_failed"]:
        print(
            f"WARNING: vault has {git['unpushed_commits']} unpushed commit(s) "
            f"vs {git['upstream'] or 'unknown upstream'}"
            + (f" — last push failed: {git['last_push_error']}" if git["last_push_error"] else ""),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
