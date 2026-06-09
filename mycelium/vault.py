"""Disk-first vault operations.

Markdown files with YAML frontmatter are the canonical record.
ChromaDB is a derived search index, rebuildable from disk.

Vault layout:
  vault/notes/    — curated synthesis notes      ({slug}.md)
  vault/drawers/  — verbatim captures             (drawer_{id}.md)
  vault/diary/    — session diary entries         ({YYYY-MM-DD}.md)
  vault/concepts/ — free-form concept pages       ({slug}.md)
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter

from mycelium.config import (
    CONCEPTS_DIR,
    DIARY_DIR,
    DRAWERS_DIR,
    LINKS_DIR,
    NOTES_DIR,
    VAULT_DIR,
    VAULT_GIT_AUTHOR_EMAIL,
    VAULT_GIT_AUTHOR_NAME,
)


# ---------------------------------------------------------------------------
# Git auto-commit
# ---------------------------------------------------------------------------

_GIT_LOG = Path.home() / ".mycelium" / "git.log"


def _git_args() -> list[str]:
    """Base git args with safety + identity overrides as -c flags so the
    function works in any execution context (host or container, any UID).
    """
    vault = str(VAULT_DIR)
    return [
        "git",
        "-c", f"safe.directory={vault}",
        "-c", f"user.name={VAULT_GIT_AUTHOR_NAME}",
        "-c", f"user.email={VAULT_GIT_AUTHOR_EMAIL}",
        "-C", vault,
    ]


def _git_commit(message: str) -> None:
    """Stage and commit current vault changes, then fire an async push.

    Push happens in the background so the write path isn't blocked on
    network I/O. Failures are logged to ~/.mycelium/git.log rather than
    swallowed silently.
    """
    if not VAULT_GIT_AUTHOR_NAME:
        return
    if not (VAULT_DIR / ".git").exists():
        return  # vault is not a git repo (expected during first-write before init)
    try:
        subprocess.run(_git_args() + ["add", "-A"], check=True, capture_output=True)
        result = subprocess.run(
            _git_args() + [
                "commit", "-m", message,
                f"--author={VAULT_GIT_AUTHOR_NAME} <{VAULT_GIT_AUTHOR_EMAIL}>",
            ],
            capture_output=True,
        )
        # Exit code 1 with "nothing to commit" is normal (no changes staged).
        if result.returncode == 0:
            _git_push_async()
        elif b"nothing to commit" not in result.stdout + result.stderr:
            _log_git_failure(message, result.stderr.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError) as e:
        _log_git_failure(message, str(e))


def _git_push_async() -> None:
    """Fire-and-forget `git push origin HEAD`. Output appended to git.log.

    Uses Popen with no wait so callers return as soon as the local commit
    is done. The child process keeps writing to the log fd after the
    parent closes its handle (fd is dup'd at fork time).
    """
    try:
        _GIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_fh = open(_GIT_LOG, "a")
        try:
            log_fh.write(
                f"[{datetime.now(timezone.utc).isoformat()}] push initiated\n"
            )
            log_fh.flush()
            subprocess.Popen(
                _git_args() + ["push", "origin", "HEAD"],
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=True,
            )
        finally:
            log_fh.close()
    except (OSError, subprocess.SubprocessError) as e:
        _log_git_failure("push", str(e))


def _log_git_failure(message: str, error: str) -> None:
    try:
        _GIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_GIT_LOG, "a") as f:
            f.write(f"[{datetime.now(timezone.utc).isoformat()}] commit '{message}' failed: {error.strip()}\n")
    except OSError:
        pass


def init_vault_git() -> None:
    """Ensure vault/ is a git repo. Safe to call multiple times."""
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    git_dir = VAULT_DIR / ".git"
    if not git_dir.exists():
        subprocess.run(_git_args() + ["init"], check=True, capture_output=True)


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^\w\s-]")
_WS_RE   = re.compile(r"[\s_-]+")


def slugify(title: str) -> str:
    s = title.lower().strip()
    s = _SLUG_RE.sub("", s)
    s = _WS_RE.sub("-", s)
    return s[:80]


def note_id(slug: str) -> str:
    return "note_" + hashlib.sha256(slug.encode()).hexdigest()[:16]


def drawer_id(content: str, wing: str, room: str) -> str:
    key = f"{wing}:{room}:{content[:200]}"
    return "drawer_" + hashlib.sha256(key.encode()).hexdigest()[:16]


def link_id(source_id: str, relation_type: str, target_id: str) -> str:
    key = f"{source_id}:{relation_type}:{target_id}"
    return "link_" + hashlib.sha256(key.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Notes (curated synthesis)
# ---------------------------------------------------------------------------

def write_note(
    title: str,
    content: str,
    tags: list[str] | None = None,
    source_memories: list[str] | None = None,
    status: str | None = None,
    author: str | None = None,
    intent: str | None = None,
    committed: bool = True,
) -> tuple[str, Path]:
    """Write or upsert a curated note to disk.

    Frontmatter fields (v1.0.5+):
        id, title, tags, source_memories, created, updated, status,
        author, committed_at, source_intent

    `status` is for notes that represent tracked work. Pass None to leave the
    existing value alone on upsert (or to omit the field on first write).
    Pass "" to clear. Canonical values: "open" | "in-progress" | "done" |
    "wont-fix" | "blocked".

    `created` is preserved across upserts (read from existing file if present).

    `author` defaults to mycelium.config.AUTHOR (MYCELIUM_AUTHOR env or git
    user.email or "unknown"). In team mode (Phase 3.3+), the per-call author
    is derived from the auth context.

    `intent` is the agent's reasoning about why this note exists. Currently
    optional; will be required for write_note in v2.0.0.

    `committed` controls the committed_at frontmatter flag. True (default)
    stamps a fresh UTC timestamp synchronously — matches today's personal-mode
    semantics. False writes `committed_at: null`, signalling a team-mode draft
    awaiting the writer worker's chroma upsert.
    """
    from mycelium.write_path.frontmatter import stamp_author, stamp_committed, stamp_intent

    tags = tags or []
    source_memories = source_memories or []

    slug  = slugify(title)
    nid   = note_id(slug)
    now   = datetime.now(timezone.utc).isoformat()

    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    filepath = NOTES_DIR / f"{slug}.md"

    created = now
    existing_status: str | None = None
    if filepath.exists():
        try:
            existing = frontmatter.load(str(filepath))
            created = existing.get("created", now)
            existing_status = existing.get("status")
        except Exception:
            pass

    final_status = existing_status if status is None else status

    fields: dict[str, Any] = {
        "id":              nid,
        "title":           title,
        "created":         created,
        "updated":         now,
        "tags":            tags,
        "source_memories": source_memories,
    }
    if final_status:
        fields["status"] = final_status

    stamp_author(fields, author)
    stamp_committed(fields, committed=committed)
    stamp_intent(fields, intent)

    post = frontmatter.Post(content, **fields)
    filepath.write_text(frontmatter.dumps(post), encoding="utf-8")
    _git_commit(f"note: {title}")
    return nid, filepath


def load_note(filepath: Path) -> dict[str, Any] | None:
    try:
        post = frontmatter.load(str(filepath))
        return {
            "note_id":         post.get("id", note_id(slugify(post.get("title", filepath.stem)))),
            "title":           post.get("title", filepath.stem),
            "tags":            post.get("tags", []),
            "source_memories": post.get("source_memories", []),
            "created_at":      post.get("created", ""),
            "updated_at":      post.get("updated", ""),
            "status":          post.get("status", ""),
            "author":          post.get("author", ""),
            "content":         post.content,
            "filepath":        str(filepath),
        }
    except Exception:
        return None


def delete_note(nid: str) -> bool:
    """Delete a note from disk by note_id. Returns True if found and deleted."""
    if not NOTES_DIR.exists():
        return False
    for f in NOTES_DIR.glob("*.md"):
        note = load_note(f)
        if note and note["note_id"] == nid:
            f.unlink()
            _git_commit(f"delete note: {note['title']}")
            return True
    return False


# ---------------------------------------------------------------------------
# Drawers (verbatim captures)
# ---------------------------------------------------------------------------

def write_drawer(
    content: str,
    wing: str,
    room: str,
    author: str | None = None,
    committed: bool = True,
    source: str = "",
) -> tuple[str, Path]:
    """Write a verbatim drawer to disk and return (drawer_id, filepath).

    See write_note for `author` / `committed` semantics. Drawers are verbatim
    captures and don't accept `source_intent` — no synthesis reasoning to attach.
    `source` names the raw origin the content was copied from (command, file,
    paste, transcript); it is stamped into frontmatter so verbatim-ness stays
    auditable. It does NOT feed drawer_id — provenance, not identity.
    """
    from mycelium.write_path.frontmatter import stamp_author, stamp_committed

    did   = drawer_id(content, wing, room)
    now   = datetime.now(timezone.utc).isoformat()

    DRAWERS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = DRAWERS_DIR / f"{did}.md"

    fields: dict[str, Any] = {
        "id":       did,
        "wing":     wing,
        "room":     room,
        "filed_at": now,
    }
    if source:
        fields["source"] = source
    stamp_author(fields, author)
    stamp_committed(fields, committed=committed)

    post = frontmatter.Post(content, **fields)
    filepath.write_text(frontmatter.dumps(post), encoding="utf-8")
    _git_commit(f"drawer: {wing}/{room}")
    return did, filepath


def update_drawer(did: str, content: str) -> Path | None:
    filepath = DRAWERS_DIR / f"{did}.md"
    if not filepath.exists():
        return None
    try:
        post = frontmatter.load(str(filepath))
    except Exception:
        return None
    post.content = content
    post["updated_at"] = datetime.now(timezone.utc).isoformat()
    filepath.write_text(frontmatter.dumps(post), encoding="utf-8")
    _git_commit(f"update drawer: {did}")
    return filepath


def delete_drawer(did: str) -> bool:
    filepath = DRAWERS_DIR / f"{did}.md"
    if not filepath.exists():
        return False
    filepath.unlink()
    _git_commit(f"delete drawer: {did}")
    return True


def get_drawer(did: str) -> dict[str, Any] | None:
    filepath = DRAWERS_DIR / f"{did}.md"
    if not filepath.exists():
        return None
    try:
        post = frontmatter.load(str(filepath))
        return {
            "drawer_id": post.get("id", did),
            "wing":      post.get("wing", "unknown"),
            "room":      post.get("room", "unknown"),
            "filed_at":  post.get("filed_at", ""),
            "author":    post.get("author", ""),
            "content":   post.content,
            "filepath":  str(filepath),
        }
    except Exception:
        return None


def list_wings() -> list[str]:
    if not DRAWERS_DIR.exists():
        return []
    wings: set[str] = set()
    for f in DRAWERS_DIR.glob("*.md"):
        try:
            post = frontmatter.load(str(f))
            w = post.get("wing")
            if w:
                wings.add(w)
        except Exception:
            pass
    return sorted(wings)


def list_rooms(wing: str) -> list[str]:
    if not DRAWERS_DIR.exists():
        return []
    rooms: set[str] = set()
    for f in DRAWERS_DIR.glob("*.md"):
        try:
            post = frontmatter.load(str(f))
            if post.get("wing") == wing:
                r = post.get("room")
                if r:
                    rooms.add(r)
        except Exception:
            pass
    return sorted(rooms)


def list_drawers(wing: str | None = None, room: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    if not DRAWERS_DIR.exists():
        return []
    results = []
    for f in sorted(DRAWERS_DIR.glob("*.md"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            post = frontmatter.load(str(f))
            if wing and post.get("wing") != wing:
                continue
            if room and post.get("room") != room:
                continue
            results.append({
                "drawer_id": post.get("id", f.stem),
                "wing":      post.get("wing", "unknown"),
                "room":      post.get("room", "unknown"),
                "filed_at":  post.get("filed_at", ""),
                "preview":   post.content[:120].replace("\n", " "),
            })
            if len(results) >= limit:
                break
        except Exception:
            pass
    return results


# ---------------------------------------------------------------------------
# Links (typed semantic edges, persisted disk-first)
# ---------------------------------------------------------------------------

def write_link(
    source_id: str, source_type: str, source_label: str,
    target_id: str, target_type: str, target_label: str,
    relation_type: str, description: str,
    ended_at: str = "",
    author: str | None = None,
    committed: bool = True,
) -> tuple[str, Path]:
    """Persist a typed link as vault/links/{link_id}.md. Frontmatter holds the
    structured fields; the body is empty (links have no narrative content).
    Returns (link_id, filepath).

    See write_note for `author` / `committed` semantics. Links don't accept
    `intent` (no synthesis content to attach reasoning to).
    """
    from mycelium.write_path.frontmatter import stamp_author, stamp_committed

    lid = link_id(source_id, relation_type, target_id)
    now = datetime.now(timezone.utc).isoformat()

    LINKS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = LINKS_DIR / f"{lid}.md"

    # Preserve created_at if file already exists (so re-upsert doesn't reset it)
    created_at = now
    if filepath.exists():
        try:
            existing = frontmatter.load(str(filepath))
            created_at = existing.get("created_at", now)
        except Exception:
            pass

    fields: dict[str, Any] = {
        "link_id":       lid,
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
    }
    stamp_author(fields, author)
    stamp_committed(fields, committed=committed)

    post = frontmatter.Post("", **fields)
    filepath.write_text(frontmatter.dumps(post), encoding="utf-8")
    _git_commit(f"link: {source_label} --[{relation_type}]--> {target_label}")
    return lid, filepath


def load_link(filepath: Path) -> dict[str, Any] | None:
    try:
        post = frontmatter.load(str(filepath))
        return {
            "link_id":       post.get("link_id", filepath.stem),
            "source_id":     post.get("source_id", ""),
            "source_type":   post.get("source_type", ""),
            "source_label":  post.get("source_label", ""),
            "target_id":     post.get("target_id", ""),
            "target_type":   post.get("target_type", ""),
            "target_label":  post.get("target_label", ""),
            "relation_type": post.get("relation_type", ""),
            "description":   post.get("description", ""),
            "created_at":    post.get("created_at", ""),
            "ended_at":      post.get("ended_at", ""),
            "filepath":      str(filepath),
        }
    except Exception:
        return None


def delete_link_file(lid: str) -> bool:
    filepath = LINKS_DIR / f"{lid}.md"
    if not filepath.exists():
        return False
    filepath.unlink()
    _git_commit(f"delete link: {lid}")
    return True


# ---------------------------------------------------------------------------
# Diary
# ---------------------------------------------------------------------------

# Diary entries are filed per-author-per-day: vault/diary/{date}-{slug}.md, one
# file per (calendar day, author). The derived drawer id is `diary_` + the
# filename stem (e.g. diary_2026-06-05-liam.blignaut), so every index / orphan /
# reindex site that builds `diary_{f.stem}` stays correct with no date/slug
# parsing of its own.
DIARY_WING = "wing_claude"
DIARY_ROOM = "diary"

_DIARY_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _author_slug(author: str) -> str:
    """Filesystem-safe slug from an author identity for the diary filename.

    Uses the email local-part when present (liam.blignaut@rain.co.za ->
    liam.blignaut); lowercased, non-alphanumerics collapsed to '.'. The full
    identity is preserved in the file's `author` frontmatter.
    """
    local = author.split("@", 1)[0] if "@" in author else author
    slug = re.sub(r"[^a-z0-9]+", ".", local.lower()).strip(".")
    return slug or "unknown"


def _diary_date_of(stem: str) -> str:
    """Calendar-date prefix of a diary filename stem.

    '2026-06-05-liam.blignaut' -> '2026-06-05'; legacy '2026-06-05' -> itself.
    """
    m = _DIARY_DATE_RE.match(stem)
    return m.group(1) if m else stem


def diary_write(content: str, session_id: str = "", author: str | None = None) -> Path:
    """Append a diary entry for today into a per-author-per-day file.

    Files are `vault/diary/{YYYY-MM-DD}-{slug}.md`, one per (calendar day,
    author), carrying YAML frontmatter (author / date / wing / room /
    committed_at / session_ids). Each entry is delimited and keeps a per-entry
    HTML comment with its own timestamp + session id.

    Author resolves via identity.resolve_author() — fail-closed in team mode,
    config.AUTHOR in personal mode — so the diary obeys the same provenance rule
    as every other write path. Re-indexes the file into the drawers collection
    so diary content surfaces in search/context.
    """
    from mycelium.write_path.frontmatter import now_iso

    if author is None:
        from mycelium.identity import resolve_author
        author = resolve_author()

    slug = _author_slug(author)
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = DIARY_DIR / f"{today}-{slug}.md"

    now = now_iso()
    entry = f"\n\n---\n<!-- {now} session={session_id} author={author} -->\n{content}"

    if filepath.exists():
        post = frontmatter.load(str(filepath))
        post.content = post.content + entry
        sessions = list(post.get("session_ids") or [])
        if session_id and session_id not in sessions:
            sessions.append(session_id)
        post["session_ids"] = sessions
        post["committed_at"] = now
    else:
        post = frontmatter.Post(
            f"# Diary {today} — {slug}{entry}",
            id=f"diary_{today}-{slug}",
            author=author,
            date=today,
            wing=DIARY_WING,
            room=DIARY_ROOM,
            committed_at=now,
            session_ids=[session_id] if session_id else [],
        )

    filepath.write_text(frontmatter.dumps(post), encoding="utf-8")
    _git_commit(f"diary: {today} ({slug})")
    _index_diary_day(filepath)
    return filepath


def _index_diary_day(filepath: Path) -> None:
    """Upsert a diary file's body into the drawers collection so it's searchable.

    Reads author/wing from frontmatter; drawer id = `diary_` + filename stem
    (stable + idempotent). The YAML frontmatter is excluded from the embedded
    document — only the diary body is indexed."""
    try:
        from mycelium.chroma import drawers_collection
        post = frontmatter.load(str(filepath))
        diary_id = f"diary_{filepath.stem}"
        drawers_collection().upsert(
            ids=[diary_id],
            documents=[post.content],
            metadatas=[{
                "drawer_id":   diary_id,
                "wing":        post.get("wing", DIARY_WING),
                "room":        post.get("room", DIARY_ROOM),
                "filed_at":    post.get("date", _diary_date_of(filepath.stem)),
                "author":      post.get("author", ""),
                "source_file": str(filepath),
            }],
        )
    except Exception:
        pass  # don't let index failure break the write


def diary_read(date: str | None = None, n_days: int = 3) -> str:
    """Read diary entries across per-author files.

    date='YYYY-MM-DD' returns every author's file for that day, merged. With no
    date, returns the most recent `n_days` *calendar days* (all authors per day),
    newest day first, authors A->Z within a day.
    """
    if not DIARY_DIR.exists():
        return "No diary entries yet."

    if date:
        files = sorted(DIARY_DIR.glob(f"{date}-*.md"))
        legacy = DIARY_DIR / f"{date}.md"          # pre-split single-file days
        if legacy.exists():
            files = [legacy, *files]
        if not files:
            return f"No entry for {date}."
        return "\n\n".join(f.read_text(encoding="utf-8") for f in files)

    files = list(DIARY_DIR.glob("*.md"))
    if not files:
        return "No diary entries yet."
    by_date: dict[str, list[Path]] = {}
    for f in files:
        by_date.setdefault(_diary_date_of(f.stem), []).append(f)
    out: list[str] = []
    for d in sorted(by_date, reverse=True)[:n_days]:      # newest day first
        for f in sorted(by_date[d]):                       # authors A->Z within a day
            out.append(f.read_text(encoding="utf-8"))
    return "\n\n".join(out)


# ---------------------------------------------------------------------------
# Index rebuild (ChromaDB from disk)
# ---------------------------------------------------------------------------

REINDEX_BATCH_SIZE = 500

# Chunk drawers larger than this when indexing into ChromaDB. Each chunk
# gets its own embedding so a long document can be matched on any of its
# subsections, then expanded back to full content via get_drawer().
CHUNK_THRESHOLD = 2000   # chars; smaller drawers index as a single chunk
CHUNK_SIZE      = 2000   # target max chars per chunk
CHUNK_OVERLAP   = 200    # overlap to bridge concept boundaries


# ---------------------------------------------------------------------------
# Closet generation
# ---------------------------------------------------------------------------

# Closets group drawers by topical cluster so a query that matches the
# cluster's collective vibe can boost any drawer in it. One closet per
# (wing, room) for now — finer-grained topic clustering can replace this
# later. The closet document format follows mempalace's:
#   "topic|entities|→drawer_id_a,drawer_id_b,..."
# multiple lines per closet stack additional topics. We emit a single line
# per (wing, room) cluster.

_ENTITY_RE = re.compile(r"(?:[A-Z][a-zA-Z0-9]{2,})|(?:[a-z_][a-z0-9_]{4,})", re.UNICODE)


def _extract_entities(content: str, max_entities: int = 12) -> list[str]:
    """Pull a small set of distinguishing tokens (CamelCase or snake_case)
    that are likely to be entity-ish — names, identifiers, technical terms.
    """
    seen: dict[str, None] = {}
    for match in _ENTITY_RE.finditer(content):
        tok = match.group()
        if tok.lower() in seen:
            continue
        seen[tok.lower()] = None
        if len(seen) >= max_entities:
            break
    return list(seen.keys())


def closet_id(wing: str, room: str) -> str:
    key = f"{wing}:{room}"
    return "closet_" + hashlib.sha256(key.encode()).hexdigest()[:16]


def regenerate_closets() -> tuple[int, int]:
    """Rebuild the closets collection from scratch using current drawers.

    One closet per (wing, room) cluster. Boost is granted to all drawers in
    that cluster when the cluster's topic matches the query. Orphan closets
    (whose wing/room has no drawers) are pruned.

    Returns (upserted, deleted_orphans).
    """
    from mycelium.chroma import closets_collection

    col = closets_collection()

    # Group drawers by (wing, room) from disk
    clusters: dict[tuple[str, str], list[tuple[str, str]]] = {}
    if DRAWERS_DIR.exists():
        for f in DRAWERS_DIR.glob("*.md"):
            d = get_drawer(f.stem)
            if not d:
                continue
            key = (d["wing"], d["room"])
            clusters.setdefault(key, []).append((d["drawer_id"], d["content"]))
    if DIARY_DIR.exists():
        for f in DIARY_DIR.glob("*.md"):
            try:
                post = frontmatter.load(str(f))
                wing, body = post.get("wing", DIARY_WING), post.content[:2000]
            except Exception:
                wing, body = DIARY_WING, f.read_text(encoding="utf-8")[:2000]
            key = (wing, DIARY_ROOM)
            clusters.setdefault(key, []).append((f"diary_{f.stem}", body))

    disk_ids = {closet_id(wing, room) for (wing, room) in clusters.keys()}
    indexed_ids = set(col.get(include=[])["ids"])
    orphans = list(indexed_ids - disk_ids)
    if orphans:
        col.delete(ids=orphans)

    import time
    t0 = time.time()
    total = len(clusters)
    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []
    upserted = 0

    def _flush() -> None:
        nonlocal upserted
        if not batch_ids:
            return
        col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
        upserted += len(batch_ids)
        print(_progress("closets", upserted, total, t0), flush=True)
        batch_ids.clear(); batch_docs.clear(); batch_metas.clear()

    for (wing, room), members in clusters.items():
        drawer_ids = [did for did, _ in members]
        # Sample text from a few members for entity extraction (cap to avoid huge inputs)
        sample_text = " ".join(content[:800] for _, content in members[:10])
        entities = _extract_entities(sample_text)
        topic = f"{wing}/{room}"
        # Closet document format mirrors mempalace: topic|entities|→id1,id2,...
        doc = f"{topic}|{';'.join(entities)}|→{','.join(drawer_ids)}"
        batch_ids.append(closet_id(wing, room))
        batch_docs.append(doc)
        batch_metas.append({
            "wing":         wing,
            "room":         room,
            "topic":        topic,
            "entities":     ";".join(entities),
            "drawer_count": len(drawer_ids),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
        if len(batch_ids) >= REINDEX_BATCH_SIZE:
            _flush()
    _flush()
    return upserted, len(orphans)


def update_closet_for_drawer(drawer_id: str, wing: str, room: str, content: str) -> None:
    """Incrementally add a new drawer to its (wing, room) closet.

    Called from write_drawer so freshly filed content gets closet coverage
    without waiting for a full regenerate_closets pass. If the closet exists,
    appends the drawer_id to its pointer list and refreshes entities. If not,
    creates one.
    """
    from mycelium.chroma import closets_collection

    col   = closets_collection()
    cid   = closet_id(wing, room)
    topic = f"{wing}/{room}"

    existing = col.get(ids=[cid], include=["documents", "metadatas"])
    drawer_ids: list[str] = []
    entities_set: dict[str, None] = {}
    if existing.get("ids"):
        doc = (existing["documents"] or [""])[0] or ""
        for match in _CLOSET_DRAWER_REF_RE.findall(doc):
            for did in match.split(","):
                did = did.strip()
                if did and did not in drawer_ids:
                    drawer_ids.append(did)
        meta = (existing.get("metadatas") or [{}])[0] or {}
        for e in (meta.get("entities") or "").split(";"):
            if e:
                entities_set[e] = None

    if drawer_id not in drawer_ids:
        drawer_ids.append(drawer_id)

    for e in _extract_entities(content):
        if len(entities_set) >= 12:
            break
        entities_set.setdefault(e, None)
    entities = list(entities_set.keys())

    doc = f"{topic}|{';'.join(entities)}|→{','.join(drawer_ids)}"
    col.upsert(
        ids=[cid],
        documents=[doc],
        metadatas=[{
            "wing":         wing,
            "room":         room,
            "topic":        topic,
            "entities":     ";".join(entities),
            "drawer_count": len(drawer_ids),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }],
    )


# Closet pointer regex shared with search.py for parsing closet documents
_CLOSET_DRAWER_REF_RE = re.compile(r"→([\w,]+)")


def chunk_content(content: str, max_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split content into chunks for embedding. Prefers paragraph boundaries.

    Returns a single-element list if content is small enough; otherwise
    overlapping chunks of approximately max_size chars each.
    """
    if len(content) <= CHUNK_THRESHOLD:
        return [content]

    chunks: list[str] = []
    pos = 0
    n = len(content)
    while pos < n:
        end = min(pos + max_size, n)
        if end < n:
            # Try to break at a paragraph boundary near the target end
            cut = content.rfind("\n\n", pos, end)
            if cut == -1 or cut < pos + max_size // 2:
                cut = content.rfind("\n", pos, end)
            if cut == -1 or cut < pos + max_size // 2:
                cut = content.rfind(". ", pos, end)
            if cut != -1 and cut > pos + max_size // 2:
                end = cut
        chunks.append(content[pos:end].strip())
        if end >= n:
            break
        pos = max(end - overlap, pos + 1)
    return [c for c in chunks if c]


def _progress(label: str, done: int, total: int, t0: float) -> str:
    """Format a progress line with rate + ETA."""
    import time
    elapsed = max(time.time() - t0, 0.001)
    rate = done / elapsed
    if rate > 0 and done < total:
        eta_sec = (total - done) / rate
        eta = f"eta {eta_sec/60:.0f}m" if eta_sec > 60 else f"eta {eta_sec:.0f}s"
    else:
        eta = "done" if done >= total else "?"
    pct = 100 * done / total if total else 0
    return f"  {label}: {done}/{total} ({pct:.0f}%) @ {rate:.0f}/s {eta}"


def _concept_id(slug: str) -> str:
    return "concept_" + hashlib.sha256(slug.encode()).hexdigest()[:16]


def reindex_notes() -> tuple[int, int]:
    """Sync mycelium_notes ChromaDB collection with vault/notes/ + vault/concepts/.

    Streams files in batches — loads, upserts, frees memory. Concepts are
    indexed into the same collection with metadata type="concept" so they
    surface in query_notes and context but stay distinguishable from notes.

    Returns (upserted, deleted_orphans).
    """
    from mycelium.chroma import notes_collection

    col = notes_collection()

    # Orphan check by filename → derived id (slug-based), no YAML parse
    note_disk_ids: set[str] = (
        {note_id(f.stem) for f in NOTES_DIR.glob("*.md")} if NOTES_DIR.exists() else set()
    )
    concept_disk_ids: set[str] = (
        {_concept_id(f.stem) for f in CONCEPTS_DIR.glob("*.md")} if CONCEPTS_DIR.exists() else set()
    )
    disk_ids = note_disk_ids | concept_disk_ids

    indexed_ids = set(col.get(include=[])["ids"])
    orphans = list(indexed_ids - disk_ids)
    if orphans:
        col.delete(ids=orphans)

    # Stream-upsert in batches
    import time
    total = len(disk_ids)
    t0 = time.time()
    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []
    upserted = 0

    def _flush() -> None:
        nonlocal upserted
        if not batch_ids:
            return
        col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
        upserted += len(batch_ids)
        print(_progress("notes+concepts", upserted, total, t0), flush=True)
        batch_ids.clear(); batch_docs.clear(); batch_metas.clear()

    # Notes
    for f in (NOTES_DIR.glob("*.md") if NOTES_DIR.exists() else []):
        n = load_note(f)
        if not n:
            continue
        batch_ids.append(n["note_id"])
        batch_docs.append(f"{n['title']}\n\n{n['content']}")
        meta = {
            "type":            "note",
            "title":           n["title"],
            "slug":            slugify(n["title"]),
            "tags":            json.dumps(n["tags"]),
            "source_memories": json.dumps(n["source_memories"]),
            "created_at":      n["created_at"],
            "filepath":        n["filepath"],
            "author":          n.get("author", ""),
        }
        # Propagate the `status` field from frontmatter so `where={"status":...}`
        # filter queries work after a disk-side backfill. Skip when unset so we
        # don't index empty strings as a real status value.
        if n.get("status"):
            meta["status"] = n["status"]
        batch_metas.append(meta)
        if len(batch_ids) >= REINDEX_BATCH_SIZE:
            _flush()

    # Concepts
    for f in (CONCEPTS_DIR.glob("*.md") if CONCEPTS_DIR.exists() else []):
        try:
            post = frontmatter.load(str(f))
        except Exception:
            continue
        slug  = f.stem
        title = post.get("title", slug)
        batch_ids.append(_concept_id(slug))
        batch_docs.append(f"{title}\n\n{post.content}")
        batch_metas.append({
            "type":     "concept",
            "title":    title,
            "slug":     slug,
            "tags":     json.dumps(post.get("tags", []) or []),
            "filepath": str(f),
        })
        if len(batch_ids) >= REINDEX_BATCH_SIZE:
            _flush()

    _flush()
    return upserted, len(orphans)


def reindex_drawers() -> tuple[int, int]:
    """Sync mycelium_drawers ChromaDB collection with vault/drawers/.

    Streams files in batches. Returns (upserted, deleted_orphans).
    """
    from mycelium.chroma import drawers_collection

    col = drawers_collection()

    # Disk drawer_ids (one parent per file); chroma may have multi-chunk entries
    # whose IDs look like "{drawer_id}__c{N}". Strip the suffix for the orphan
    # check so we don't accidentally prune valid chunks of existing drawers.
    drawer_disk_ids: set[str] = (
        {f.stem for f in DRAWERS_DIR.glob("*.md")} if DRAWERS_DIR.exists() else set()
    )
    diary_disk_ids: set[str] = (
        {f"diary_{f.stem}" for f in DIARY_DIR.glob("*.md")} if DIARY_DIR.exists() else set()
    )
    disk_ids = drawer_disk_ids | diary_disk_ids

    def _parent_id(chroma_id: str) -> str:
        return chroma_id.split("__c", 1)[0] if "__c" in chroma_id else chroma_id

    indexed_ids = col.get(include=[])["ids"]
    orphans = [cid for cid in indexed_ids if _parent_id(cid) not in disk_ids]
    if orphans:
        col.delete(ids=orphans)

    import time
    total = len(disk_ids)
    t0 = time.time()
    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []
    upserted = 0  # counts parent drawers processed, not chunks

    def _flush() -> None:
        if not batch_ids:
            return
        col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
        print(_progress("drawers", upserted, total, t0), flush=True)
        batch_ids.clear(); batch_docs.clear(); batch_metas.clear()

    def _enqueue_drawer(did: str, content: str, wing: str, room: str, filed_at: str, source_file: str, author: str = "") -> None:
        nonlocal upserted
        chunks = chunk_content(content)
        total_chunks = len(chunks)
        for i, chunk_text in enumerate(chunks):
            chunk_id = did if total_chunks == 1 else f"{did}__c{i}"
            batch_ids.append(chunk_id)
            batch_docs.append(chunk_text)
            batch_metas.append({
                "drawer_id":    did,
                "chunk_index":  i,
                "total_chunks": total_chunks,
                "wing":         wing,
                "room":         room,
                "filed_at":     filed_at,
                "source_file":  source_file,
                "author":       author,
            })
        upserted += 1
        if len(batch_ids) >= REINDEX_BATCH_SIZE:
            _flush()

    # Regular drawers
    for f in (DRAWERS_DIR.glob("*.md") if DRAWERS_DIR.exists() else []):
        d = get_drawer(f.stem)
        if not d:
            continue
        _enqueue_drawer(d["drawer_id"], d["content"], d["wing"], d["room"], d["filed_at"], d["filepath"], d.get("author", ""))

    # Diary day-files indexed as drawers (id = diary_ + filename stem)
    for f in (DIARY_DIR.glob("*.md") if DIARY_DIR.exists() else []):
        try:
            post = frontmatter.load(str(f))
            body, wing = post.content, post.get("wing", DIARY_WING)
            author, filed = post.get("author", ""), post.get("date", _diary_date_of(f.stem))
        except Exception:
            body, wing, author, filed = f.read_text(encoding="utf-8"), DIARY_WING, "", _diary_date_of(f.stem)
        _enqueue_drawer(f"diary_{f.stem}", body, wing, DIARY_ROOM, filed, str(f), author)

    _flush()
    return upserted, len(orphans)


def reindex_links() -> tuple[int, int]:
    """Sync mycelium_links ChromaDB collection with vault/links/.

    Returns (upserted, deleted_orphans). Links are markdown files with empty
    body — all data in frontmatter. The chroma document text is constructed
    from the same fields used by find_links so semantic search still works.
    """
    from mycelium.chroma import links_collection

    col = links_collection()

    disk_ids: set[str] = (
        {f.stem for f in LINKS_DIR.glob("*.md")} if LINKS_DIR.exists() else set()
    )
    indexed_ids = set(col.get(include=[])["ids"])
    orphans = list(indexed_ids - disk_ids)
    if orphans:
        col.delete(ids=orphans)

    import time
    total = len(disk_ids)
    t0 = time.time()
    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []
    upserted = 0

    def _flush() -> None:
        nonlocal upserted
        if not batch_ids:
            return
        col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
        upserted += len(batch_ids)
        print(_progress("links", upserted, total, t0), flush=True)
        batch_ids.clear(); batch_docs.clear(); batch_metas.clear()

    for f in (LINKS_DIR.glob("*.md") if LINKS_DIR.exists() else []):
        link = load_link(f)
        if not link:
            continue
        doc = f"{link['source_label']} {link['relation_type']} {link['target_label']}. {link['description']}"
        batch_ids.append(link["link_id"])
        batch_docs.append(doc)
        batch_metas.append({
            "source_id":     link["source_id"],
            "source_type":   link["source_type"],
            "source_label":  link["source_label"],
            "target_id":     link["target_id"],
            "target_type":   link["target_type"],
            "target_label":  link["target_label"],
            "relation_type": link["relation_type"],
            "description":   link["description"],
            "created_at":    link["created_at"],
            "ended_at":      link["ended_at"],
        })
        if len(batch_ids) >= REINDEX_BATCH_SIZE:
            _flush()
    _flush()
    return upserted, len(orphans)
