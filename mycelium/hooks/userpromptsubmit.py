#!/usr/bin/env python3
"""
Mycelium — UserPromptSubmit hook (titles-only, per-session dedup).

Fires on every prompt. Calls `mycelium-context-titles` via contextforge
HTTP and injects an index of relevant note + drawer titles. Per-session
state tracks already-surfaced IDs so each title is surfaced exactly once
per session — keeps per-prompt cost bounded and avoids redundant content
in the conversation history.

Config: ~/.mycelium/config.json
{
  "contextforge_url":    "https://nixliam.tail96a95d.ts.net",
  "contextforge_token":  "<jwt without 'Bearer ' prefix>",
  "mempalace_server_id": "e86ab056cea948c3b8ac28e0e1ca2199",
  "n_notes":             60,
  "n_drawers":           60,
  "n_links":             20,
  "max_state_ids":       2000
}

State: ~/.mycelium/state/{session_id}_surfaced.json
  { "note_ids": [...], "drawer_ids": [...], "link_ids": [...] }
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

CONFIG_PATH = Path.home() / ".mycelium" / "config.json"
STATE_DIR   = Path.home() / ".mycelium" / "state"
LOG_PATH    = Path.home() / ".mycelium" / "hook.log"

TOOL_NAME = "mycelium-context-titles"


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")


def _out(data: dict) -> None:
    print(json.dumps(data))


def _load_config() -> dict | None:
    if not CONFIG_PATH.exists():
        return None
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _sanitize(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id) or "unknown"


def _state_path(session_id: str) -> Path:
    return STATE_DIR / f"{session_id}_surfaced.json"


def _load_surfaced(session_id: str) -> dict:
    p = _state_path(session_id)
    if not p.exists():
        return {"note_ids": [], "drawer_ids": [], "link_ids": []}
    try:
        with open(p) as f:
            d = json.load(f)
        # Normalise — accept legacy formats / partial state files
        return {
            "note_ids":   list(d.get("note_ids", [])),
            "drawer_ids": list(d.get("drawer_ids", [])),
            "link_ids":   list(d.get("link_ids", [])),
        }
    except (json.JSONDecodeError, OSError):
        return {"note_ids": [], "drawer_ids": [], "link_ids": []}


def _save_surfaced(session_id: str, state: dict, max_ids: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Cap each list to prevent runaway state file growth on long sessions.
    # FIFO trim — oldest IDs drop out first, so very-stale items could
    # re-surface after the cap is hit. Acceptable for personal use.
    trimmed = {
        k: list(v[-max_ids:]) for k, v in state.items()
    }
    try:
        with open(_state_path(session_id), "w") as f:
            json.dump(trimmed, f)
    except OSError as e:
        _log(f"Failed to write state: {e}")


def _cleanup_state(max_age_days: int = 7) -> None:
    """Delete session state files older than max_age_days. Runs every hook call."""
    if not STATE_DIR.exists():
        return
    cutoff = datetime.now().timestamp() - max_age_days * 86400
    for pattern in ("*.count", "*_surfaced.json"):
        for f in STATE_DIR.glob(pattern):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


GREETING_RE = re.compile(
    r"^\s*(hey|hello|hi|sup|yo|howdy|hiya|good\s+(morning|afternoon|evening))[\s!?.]*$",
    re.IGNORECASE,
)
GREETING_FALLBACK_QUERY = "recent projects session continuity current work"


def _fetch_titles(config: dict, query: str) -> dict:
    base       = config["contextforge_url"].rstrip("/")
    server_id  = config["mempalace_server_id"]
    token      = config["contextforge_token"].removeprefix("Bearer ").strip()
    n_notes    = int(config.get("n_notes", 60))
    n_drawers  = int(config.get("n_drawers", 60))
    n_links    = int(config.get("n_links", 20))

    payload = json.dumps({
        "jsonrpc": "2.0",
        "method":  "tools/call",
        "params": {
            "name": TOOL_NAME,
            "arguments": {
                "query":     query[:250],
                "n_notes":   n_notes,
                "n_drawers": n_drawers,
                "n_links":   n_links,
            },
        },
        "id": 1,
    }).encode()

    req = Request(
        f"{base}/servers/{server_id}/mcp/",
        data=payload,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json, text/event-stream",
        },
        method="POST",
    )

    with urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()

    # StreamableHTTP may reply with SSE — extract the JSON data lines.
    if raw.lstrip().startswith("data:") or "\ndata:" in raw:
        for line in raw.splitlines():
            if line.startswith("data:"):
                obj = json.loads(line[5:].strip())
                if "result" in obj:
                    content = obj["result"].get("content", [])
                    for item in content:
                        if item.get("type") == "text":
                            return json.loads(item["text"])
        return {}

    obj = json.loads(raw)
    for item in obj.get("result", {}).get("content", []):
        if item.get("type") == "text":
            return json.loads(item["text"])
    return {}


def _dedup_and_format(titles: dict, surfaced: dict) -> tuple[str, dict]:
    """Filter out already-seen IDs, format remaining as markdown, return
    (formatted_text, ids_surfaced_this_call). Caller merges ids into state.
    """
    seen_notes   = set(surfaced["note_ids"])
    seen_drawers = set(surfaced["drawer_ids"])
    seen_links   = set(surfaced["link_ids"])

    new_notes   = [n for n in titles.get("notes",   []) if n.get("note_id")   and n["note_id"]   not in seen_notes]
    new_drawers = [d for d in titles.get("drawers", []) if d.get("drawer_id") and d["drawer_id"] not in seen_drawers]
    new_links   = [l for l in titles.get("links",   []) if l.get("link_id")   and l["link_id"]   not in seen_links]

    just_surfaced = {
        "note_ids":   [n["note_id"] for n in new_notes],
        "drawer_ids": [d["drawer_id"] for d in new_drawers],
        "link_ids":   [l["link_id"] for l in new_links],
    }

    if not new_notes and not new_drawers and not new_links:
        return "", just_surfaced

    sections: list[str] = []

    if new_notes:
        lines = []
        for n in new_notes:
            title = n.get("title", "Untitled")
            slug  = (n.get("filepath") or "").split("/")[-1].removesuffix(".md")
            tags  = n.get("tags", [])
            tag_str = f"  [{', '.join(tags)}]" if tags else ""
            lines.append(f"- [[{slug}|{title}]]{tag_str}")
        sections.append("### Notes\n" + "\n".join(lines))

    if new_drawers:
        lines = []
        for d in new_drawers:
            wing    = d.get("wing", "?")
            room    = d.get("room", "?")
            snippet = d.get("snippet", "")
            lines.append(f"- {wing}/{room} — {snippet}")
        sections.append("### Drawers\n" + "\n".join(lines))

    if new_links:
        lines = []
        for lnk in new_links:
            src = lnk.get("source", {}).get("label", "?")
            rel = lnk.get("relation_type", "?")
            tgt = lnk.get("target", {}).get("label", "?")
            lines.append(f"- {src} --[{rel}]--> {tgt}")
        sections.append("### Links\n" + "\n".join(lines))

    body = "\n\n".join(sections)
    formatted = (
        "[MYCELIUM context-index — relevant titles you haven't seen this session]\n\n"
        f"{body}\n\n"
        "[Fetch full content with mycelium-context, mycelium-query-notes, or mycelium-get-drawer when needed.]"
    )
    return formatted, just_surfaced


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", default="claude-code",
                        choices=["claude-code", "cursor"])
    args = parser.parse_args()

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        data = {}

    _cleanup_state()

    session_id = _sanitize(str(data.get("conversation_id") or data.get("session_id", "unknown")))
    prompt     = str(data.get("prompt", "")).strip()

    if not prompt:
        _out({})
        return

    is_greeting = bool(GREETING_RE.match(prompt)) or len(prompt) < 15
    effective_query = GREETING_FALLBACK_QUERY if is_greeting else prompt

    config = _load_config()
    if not config:
        _log("No config at ~/.mycelium/config.json — skipping context injection")
        _out({})
        return

    try:
        titles = _fetch_titles(config, effective_query)
        surfaced = _load_surfaced(session_id)
        formatted, just_surfaced = _dedup_and_format(titles, surfaced)

        # Always update state with what we surfaced — even when injection
        # is empty, the act of querying counts (the IDs were already
        # surfaced to the agent at some prior point).
        merged = {
            k: surfaced[k] + just_surfaced[k] for k in surfaced
        }
        max_ids = int(config.get("max_state_ids", 2000))
        _save_surfaced(session_id, merged, max_ids)

        if not formatted:
            # Quiet "all seen" case — log but don't inject. Most common
            # state for prompts that continue an existing topic.
            n_seen = sum(len(v) for v in surfaced.values())
            _log(f"[{session_id}] all relevant titles already surfaced (state has {n_seen} ids)")
            _out({})
            return

        n_notes   = len(just_surfaced["note_ids"])
        n_drawers = len(just_surfaced["drawer_ids"])
        n_links   = len(just_surfaced["link_ids"])
        _log(
            f"[{session_id}] {prompt[:60]!r} -> "
            f"+{n_notes}n / +{n_drawers}d / +{n_links}l "
            f"({len(formatted)} chars)"
        )
        _out({
            "hookSpecificOutput": {
                "hookEventName":     "UserPromptSubmit",
                "additionalContext": formatted,
            }
        })

    except (URLError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        _log(f"Error fetching titles: {exc}")
        _out({})


if __name__ == "__main__":
    main()
