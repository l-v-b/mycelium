#!/usr/bin/env python3
"""
Mycelium — UserPromptSubmit hook.

Fires on every prompt submission. On the first message of a session, calls
mycelium_context via contextforge HTTP and injects notes, memories, and links
as context before Claude responds.

Config: ~/.mycelium/config.json
{
  "contextforge_url": "https://nixliam.tail96a95d.ts.net",
  "contextforge_token": "<jwt without 'Bearer ' prefix>",
  "mempalace_server_id": "e86ab056cea948c3b8ac28e0e1ca2199",
  "search_limit": 5,
  "n_notes": 3,
  "n_links": 5
}

Usage (Claude Code settings.json):
  "UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command",
    "command": "python3 /path/to/userpromptsubmit.py --harness claude-code"}]}]
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

TOOL_NAME = "mycelium-context"


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


GREETING_RE = re.compile(
    r"^\s*(hey|hello|hi|sup|yo|howdy|hiya|good\s+(morning|afternoon|evening))[\s!?.]*$",
    re.IGNORECASE,
)
GREETING_FALLBACK_QUERY = "recent projects session continuity current work"


def _is_within_first_n_messages(session_id: str, n: int = 3) -> bool:
    """Return True (and increment count) iff fewer than n messages have been injected this session."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    counter_file = STATE_DIR / f"{session_id}.count"
    count = 0
    if counter_file.exists():
        try:
            count = int(counter_file.read_text().strip())
        except (ValueError, OSError):
            count = 0
    if count >= n:
        return False
    counter_file.write_text(str(count + 1))
    return True


def _cleanup_state(max_age_days: int = 7) -> None:
    """Delete session state files older than max_age_days. Runs on every hook call."""
    if not STATE_DIR.exists():
        return
    cutoff = datetime.now().timestamp() - max_age_days * 86400
    for f in STATE_DIR.glob("*.count"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _fetch_context(config: dict, query: str) -> dict:
    base      = config["contextforge_url"].rstrip("/")
    server_id = config["mempalace_server_id"]
    token     = config["contextforge_token"].removeprefix("Bearer ").strip()
    n_memories = int(config.get("search_limit", 5))
    n_notes    = int(config.get("n_notes", 3))
    n_links    = int(config.get("n_links", 5))

    payload = json.dumps({
        "jsonrpc": "2.0",
        "method":  "tools/call",
        "params": {
            "name": TOOL_NAME,
            "arguments": {
                "query":      query[:250],
                "n_notes":    n_notes,
                "n_drawers":  n_memories,
                "n_links":    n_links,
            },
        },
        "id": 1,
    }).encode()

    req = Request(
        f"{base}/servers/{server_id}/mcp/",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "Accept":        "application/json, text/event-stream",
        },
        method="POST",
    )

    with urlopen(req, timeout=15) as resp:
        raw = resp.read().decode()

    # StreamableHTTP can reply with SSE — extract data lines
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


def _format(ctx: dict, query: str) -> str:
    sections: list[str] = []

    notes = ctx.get("notes", [])
    if notes:
        note_texts = []
        for n in notes:
            title = n.get("title", "Untitled")
            note_id = n.get("note_id", "")
            content = n.get("content", "")
            note_texts.append(f"**{title}** (`{note_id}`)\n{content}")
        sections.append("### Notes\n\n" + "\n\n---\n\n".join(note_texts))

    memories = ctx.get("memories", [])
    if memories:
        mem_texts = [m["text"] for m in memories if m.get("type") == "text" and m.get("text", "").strip()]
        if mem_texts:
            sections.append("### Memories\n\n" + "\n\n---\n\n".join(mem_texts))

    links = ctx.get("links", [])
    if links:
        link_lines = []
        for lnk in links:
            src = lnk.get("source", {}).get("label", "?")
            rel = lnk.get("relation_type", "?")
            tgt = lnk.get("target", {}).get("label", "?")
            desc = lnk.get("description", "")
            link_lines.append(f"- {src} --[{rel}]--> {tgt}: {desc}")
        sections.append("### Links\n\n" + "\n".join(link_lines))

    if not sections:
        return ""

    body = "\n\n".join(sections)
    return (
        f"[MYCELIUM: Context retrieved for '{query[:80]}']\n\n"
        f"{body}\n\n"
        "[Use this context silently. Do not mention receiving it unless directly relevant.]"
    )


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

    if not _is_within_first_n_messages(session_id, n=3):
        _out({})
        return

    is_greeting = bool(GREETING_RE.match(prompt)) or len(prompt) < 15
    effective_query = GREETING_FALLBACK_QUERY if is_greeting else prompt
    _log(f"[{session_id}] Prompt: {prompt[:80]!r}  query: {effective_query[:80]!r}")

    config = _load_config()
    if not config:
        _log("No config at ~/.mycelium/config.json — skipping context injection")
        _out({})
        return

    try:
        ctx = _fetch_context(config, effective_query)
        context = _format(ctx, effective_query)

        if not context:
            _log("No results from mycelium_context")
            _out({})
            return

        n_notes    = len(ctx.get("notes", []))
        n_memories = len(ctx.get("memories", []))
        n_links    = len(ctx.get("links", []))
        _log(f"Injecting {n_notes} notes, {n_memories} memories, {n_links} links ({len(context)} chars)")
        _out({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        })

    except (URLError, OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        _log(f"Error during mycelium_context: {exc}")
        _out({})


if __name__ == "__main__":
    main()
