#!/usr/bin/env python3
"""
Standalone replacement for `python -m mempalace hook run --hook stop`.

Fires every SAVE_INTERVAL human messages and prompts the agent to save
session content to MemPalace. Independent of the mempalace package so it
survives `uv tool upgrade mempalace` without being overwritten.

State: ~/.mycelium/state/{session_id}_mp_last_save
Log:   ~/.mycelium/hook.log
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

SAVE_INTERVAL = 15
STATE_DIR = Path.home() / ".mycelium" / "state"
LOG_PATH  = Path.home() / ".mycelium" / "hook.log"

STOP_BLOCK_REASON = (
    "AUTO-SAVE checkpoint (MemPalace). File this session's content to MemPalace now.\n\n"
    "Default to MORE rather than less — storage is cheap, lost context is not.\n"
    "When in doubt, file it.\n\n"
    "1. mempalace_diary_write — AAAK-compressed session summary\n"
    "2. mempalace_add_drawer — verbatim content: decisions, code, config, prompts, "
    "key exchanges, anything you would not want to lose. Over-file rather than under-file.\n\n"
    "Do NOT write to Claude Code's native auto-memory (.md files). "
    "Continue conversation after saving."
)


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [mempalace_stop] {msg}\n")


def _out(data: dict) -> None:
    print(json.dumps(data))


def _sanitize(session_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "", session_id) or "unknown"


def _count_human_messages(transcript_path: str) -> int:
    path = Path(transcript_path).expanduser()
    if not path.is_file():
        return 0
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        text = (
                            content if isinstance(content, str)
                            else " ".join(
                                b.get("text", "") for b in content
                                if isinstance(b, dict)
                            )
                        )
                        if "<command-message>" not in text:
                            count += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return 0
    return count


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", default="claude-code")
    args = parser.parse_args()

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, EOFError):
        data = {}

    if str(data.get("stop_hook_active", "")).lower() in ("true", "1", "yes"):
        _out({})
        return

    session_id     = _sanitize(str(data.get("session_id", "unknown")))
    transcript_path = str(data.get("transcript_path", ""))

    exchange_count = _count_human_messages(transcript_path)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    last_save_file = STATE_DIR / f"{session_id}_mp_last_save"
    last_save = 0
    if last_save_file.is_file():
        try:
            last_save = int(last_save_file.read_text().strip())
        except (ValueError, OSError):
            last_save = 0

    since_last = exchange_count - last_save
    _log(f"Session {session_id}: {exchange_count} exchanges, {since_last} since last save")

    if since_last >= SAVE_INTERVAL and exchange_count > 0:
        try:
            last_save_file.write_text(str(exchange_count))
        except OSError:
            pass
        _log(f"Triggering save at exchange {exchange_count}")
        _out({"decision": "block", "reason": STOP_BLOCK_REASON})
    else:
        _out({})


if __name__ == "__main__":
    main()
