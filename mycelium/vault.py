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
    DIARY_DIR,
    DRAWERS_DIR,
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
    """Stage and commit current vault changes. Logs failures rather than
    swallowing them silently.
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
        if result.returncode != 0 and b"nothing to commit" not in result.stdout + result.stderr:
            _log_git_failure(message, result.stderr.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError) as e:
        _log_git_failure(message, str(e))


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
) -> tuple[str, Path]:
    """Write a curated note to disk and return (note_id, filepath)."""
    tags = tags or []
    source_memories = source_memories or []

    slug  = slugify(title)
    nid   = note_id(slug)
    now   = datetime.now(timezone.utc).isoformat()

    NOTES_DIR.mkdir(parents=True, exist_ok=True)
    filepath = NOTES_DIR / f"{slug}.md"

    post = frontmatter.Post(
        content,
        id=nid,
        title=title,
        created=now,
        updated=now,
        tags=tags,
        source_memories=source_memories,
    )
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
) -> tuple[str, Path]:
    """Write a verbatim drawer to disk and return (drawer_id, filepath)."""
    did   = drawer_id(content, wing, room)
    now   = datetime.now(timezone.utc).isoformat()

    DRAWERS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = DRAWERS_DIR / f"{did}.md"

    post = frontmatter.Post(
        content,
        id=did,
        wing=wing,
        room=room,
        filed_at=now,
    )
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
# Diary
# ---------------------------------------------------------------------------

def diary_write(content: str, session_id: str = "") -> Path:
    """Append a diary entry for today."""
    DIARY_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = DIARY_DIR / f"{today}.md"

    now = datetime.now(timezone.utc).isoformat()
    entry = f"\n\n---\n<!-- {now} session={session_id} -->\n{content}"

    if filepath.exists():
        filepath.write_text(filepath.read_text(encoding="utf-8") + entry, encoding="utf-8")
    else:
        filepath.write_text(f"# Diary {today}{entry}", encoding="utf-8")

    _git_commit(f"diary: {today}")
    return filepath


def diary_read(date: str | None = None, n_days: int = 3) -> str:
    """Read diary entries. date = 'YYYY-MM-DD' or None for recent n_days."""
    if not DIARY_DIR.exists():
        return "No diary entries yet."

    if date:
        f = DIARY_DIR / f"{date}.md"
        return f.read_text(encoding="utf-8") if f.exists() else f"No entry for {date}."

    files = sorted(DIARY_DIR.glob("*.md"), reverse=True)[:n_days]
    if not files:
        return "No diary entries yet."
    return "\n\n".join(f.read_text(encoding="utf-8") for f in files)


# ---------------------------------------------------------------------------
# Index rebuild (ChromaDB from disk)
# ---------------------------------------------------------------------------

REINDEX_BATCH_SIZE = 500


def reindex_notes() -> tuple[int, int]:
    """Sync mycelium_notes ChromaDB collection with vault/notes/.

    Streams files in batches — loads, upserts, frees memory. Returns
    (upserted, deleted_orphans).
    """
    from mycelium.chroma import notes_collection

    col = notes_collection()

    # Orphan check by filename → derived note_id (slug-based), no YAML parse
    disk_ids: set[str] = (
        {note_id(f.stem) for f in NOTES_DIR.glob("*.md")} if NOTES_DIR.exists() else set()
    )

    indexed_ids = set(col.get(include=[])["ids"])
    orphans = list(indexed_ids - disk_ids)
    if orphans:
        col.delete(ids=orphans)

    # Stream-upsert in batches
    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []
    upserted = 0
    for f in (NOTES_DIR.glob("*.md") if NOTES_DIR.exists() else []):
        n = load_note(f)
        if not n:
            continue
        batch_ids.append(n["note_id"])
        batch_docs.append(f"{n['title']}\n\n{n['content']}")
        batch_metas.append({
            "title":           n["title"],
            "slug":            slugify(n["title"]),
            "tags":            json.dumps(n["tags"]),
            "source_memories": json.dumps(n["source_memories"]),
            "created_at":      n["created_at"],
            "filepath":        n["filepath"],
        })
        if len(batch_ids) >= REINDEX_BATCH_SIZE:
            col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
            upserted += len(batch_ids)
            print(f"  notes: {upserted} indexed", flush=True)
            batch_ids.clear(); batch_docs.clear(); batch_metas.clear()
    if batch_ids:
        col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
        upserted += len(batch_ids)
        print(f"  notes: {upserted} indexed", flush=True)

    return upserted, len(orphans)


def reindex_drawers() -> tuple[int, int]:
    """Sync mycelium_drawers ChromaDB collection with vault/drawers/.

    Streams files in batches. Returns (upserted, deleted_orphans).
    """
    from mycelium.chroma import drawers_collection

    col = drawers_collection()

    # Drawer filename IS the drawer_id (no YAML parse needed for orphan check)
    disk_ids: set[str] = (
        {f.stem for f in DRAWERS_DIR.glob("*.md")} if DRAWERS_DIR.exists() else set()
    )

    indexed_ids = set(col.get(include=[])["ids"])
    orphans = list(indexed_ids - disk_ids)
    if orphans:
        col.delete(ids=orphans)

    batch_ids: list[str] = []
    batch_docs: list[str] = []
    batch_metas: list[dict] = []
    upserted = 0
    for f in (DRAWERS_DIR.glob("*.md") if DRAWERS_DIR.exists() else []):
        d = get_drawer(f.stem)
        if not d:
            continue
        batch_ids.append(d["drawer_id"])
        batch_docs.append(d["content"])
        batch_metas.append({
            "drawer_id":   d["drawer_id"],
            "wing":        d["wing"],
            "room":        d["room"],
            "filed_at":    d["filed_at"],
            "source_file": d["filepath"],
        })
        if len(batch_ids) >= REINDEX_BATCH_SIZE:
            col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
            upserted += len(batch_ids)
            print(f"  drawers: {upserted} indexed", flush=True)
            batch_ids.clear(); batch_docs.clear(); batch_metas.clear()
    if batch_ids:
        col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
        upserted += len(batch_ids)
        print(f"  drawers: {upserted} indexed", flush=True)

    return upserted, len(orphans)
