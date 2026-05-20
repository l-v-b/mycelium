#!/usr/bin/env python3
"""
Mycelium stop hook — fully standalone, no mempalace dependency.

Runs alongside mempalace's stop hook as a second independent entry in the
Stop event list. Fires every NOTE_INTERVAL human messages and asks Claude
to review the conversation for stable conclusions worth synthesising into
mycelium notes.

State: ~/.mycelium/state/{session_id}_last_note_review
Log:   ~/.mycelium/hook.log

Usage (settings.json / hooks.json):
  command: python3 /path/to/stop.py --harness claude-code
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

NOTE_INTERVAL = 30
STATE_DIR = Path.home() / ".mycelium" / "state"
LOG_PATH   = Path.home() / ".mycelium" / "hook.log"

NOTE_BLOCK_REASON = (
    "MYCELIUM NOTE REVIEW checkpoint.\n"
    "Review the conversation since the last checkpoint for stable conclusions "
    "(decisions settled, principles confirmed, architecture choices locked in, lessons learned).\n"
    "If anything qualifies:\n"
    "  1. Call mycelium_query_notes first — check if a related note already exists.\n"
    "  2. If one exists — update it with mycelium_write_note using the SAME title (upserts in place).\n"
    "  3. If nothing exists — write a new note.\n"
    "  4. Check for new typed links worth capturing (mycelium_add_link).\n"
    "     Connect notes, drawers, or concepts that are related but not yet linked.\n"
    "  Not for brainstorming, rough ideas, or things that haven't settled.\n"
    "If nothing has solidified, just say so and continue.\n"
    "Do NOT write to Claude Code's native auto-memory (.md files)."
)


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] mycelium-stop: {msg}\n")


def _sanitize(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id) or "unknown"


def _text_from_message_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return " ".join(parts)
    return ""


def _count_human_messages(transcript_path: str) -> int:
    path = Path(transcript_path).expanduser()
    if not transcript_path.strip() or not path.is_file():
        return 0
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    # Cursor Agent jsonl: {"role":"user","message":{"content":[...]}}
                    if entry.get("role") == "user":
                        msg = entry.get("message", {})
                        text = _text_from_message_content(
                            msg.get("content", "") if isinstance(msg, dict) else ""
                        )
                        if text and "<command-message>" not in text:
                            count += 1
                        continue

                    msg = entry.get("message", {})
                    # Claude Code / alternate shapes: role nested under message
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        text = (
                            content
                            if isinstance(content, str)
                            else _text_from_message_content(content)
                        )
                        if "<command-message>" not in text:
                            count += 1
                    elif entry.get("type") == "event_msg":
                        payload = entry.get("payload", {})
                        if isinstance(payload, dict) and payload.get("type") == "user_message":
                            msg_text = payload.get("message", "")
                            if isinstance(msg_text, str) and "<command-message>" not in msg_text:
                                count += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return 0
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", default="claude-code",
                        choices=["claude-code", "cursor"])
    args = parser.parse_args()

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        data = {}

    # Prevent infinite loops — same pattern as mempalace
    if str(data.get("stop_hook_active", "")).lower() in ("true", "1", "yes"):
        print("{}")
        return

    raw_sid = data.get("conversation_id") or data.get("session_id") or "unknown"
    session_id = _sanitize(str(raw_sid))
    tp = data.get("transcript_path")
    transcript = tp if isinstance(tp, str) else ""

    count = _count_human_messages(transcript)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker = STATE_DIR / f"{session_id}_last_note_review"
    last = 0
    if marker.exists():
        try:
            last = int(marker.read_text().strip())
        except (ValueError, OSError):
            last = 0

    since = count - last
    _log(f"session={session_id} count={count} last={last} since={since}")

    if since >= NOTE_INTERVAL and count > 0:
        marker.write_text(str(count))
        _log(f"triggering note review at message {count}")
        if args.harness == "cursor":
            print(json.dumps({"followup_message": NOTE_BLOCK_REASON}))
        else:
            print(json.dumps({"decision": "block", "reason": NOTE_BLOCK_REASON}))
    else:
        print("{}")


if __name__ == "__main__":
    main()
