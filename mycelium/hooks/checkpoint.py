#!/usr/bin/env python3
"""
Mycelium checkpoint hook — merged verbatim + synthesis.

Replaces verbatim_stop.py and stop.py. Fires every CHECKPOINT_INTERVAL human
turns and prompts the agent to run a 4-pass capture:
  1. Enumerate everything since last checkpoint (no filing, no judgment).
  2. File each item verbatim (mycelium-file, one drawer per item).
  3. Write the AAAK diary entry (mycelium-diary-write).
  4. Synthesise stable conclusions into notes (judgment allowed here only).

The hook passes the actual since-last-checkpoint window (human-turn indices
and count) into the prompt so the agent isn't guessing the boundary.

State: ~/.mycelium/state/{session_id}_last_checkpoint
Log:   ~/.mycelium/hook.log

Usage (settings.json):
  command: python3 /home/liam/.mycelium/hooks/checkpoint.py --harness claude-code
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

CHECKPOINT_INTERVAL = 12
STATE_DIR = Path.home() / ".mycelium" / "state"
LOG_PATH  = Path.home() / ".mycelium" / "hook.log"


def _build_reason(window_start: int, window_end: int, since: int) -> str:
    return (
        f"MYCELIUM CHECKPOINT — file human turns {window_start}–{window_end} "
        f"({since} turns since last checkpoint) now.\n"
        "This is a full capture pass, not a highlights reel. It is not optional.\n\n"
        "PASS 1 — ENUMERATE (no filing yet)\n"
        "List every distinct item from that window: each decision, code block, "
        "config/command + its output, prompt or template, error + root cause, and any "
        "substantive exchange or rationale. Enumerate everything BEFORE filing anything. "
        "Do not weigh importance in this pass — just list.\n\n"
        "PASS 2 — FILE VERBATIM (no skipping)\n"
        "Every enumerated item's full content must be filed verbatim with mycelium-file. "
        "You MAY group tightly-related items into one drawer — but never summarise, never "
        "drop, and never let a decision live only inside an artifact that shows its outcome "
        "(a filed code change is not a substitute for filing the decision behind it — same "
        "class of error as diary≠drawer, one level up).\n"
        "- Scope wing/room to the NARROWEST accurate names so scope-aware retrieval can "
        "find it later. If unsure of existing names, call mycelium-get-taxonomy once first.\n"
        "- A diary entry is NOT a substitute for a drawer. The diary records THAT something "
        "happened; the drawer is the only thing that preserves WHAT it was.\n"
        "- End Pass 2 with a reconciliation line: \"Pass 1 enumerated N; filed M drawers; "
        "merges: [list]; nothing dropped.\" Consolidation must be an auditable statement, "
        "not a silent act — a real drop should surface as \"filed M, dropped K\" instead of "
        "hiding in the gap.\n\n"
        "PASS 3 — DIARY\n"
        "Write one AAAK-compressed session summary with mycelium-diary-write. "
        "This indexes the session. It does not replace Pass 2.\n\n"
        "PASS 4 — SYNTHESIS (judgment allowed here, and only here)\n"
        "For stable conclusions only — decisions settled, principles confirmed, "
        "architecture locked in, lessons learned:\n"
        "  1. Call mycelium-context-titles to check for a related existing note "
        "(semantically ranked, titles-only — cheap).\n"
        "  2. Match → upsert with mycelium-write-note using the SAME title. "
        "No match → new note. If a title looks close but you're unsure, fetch it "
        "before deciding.\n"
        "  3. Add typed links (mycelium-add-link) for new relationships.\n"
        "Skip this pass if nothing has solidified — but NEVER skip Pass 2.\n\n"
        "Do not write to Claude Code native auto-memory (.md). Continue after filing."
    )


def _log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a") as f:
        f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [checkpoint] {msg}\n")


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
                    # Claude Code shape: role nested under message
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        # Tool results are ALSO role=user in Claude Code's
                        # transcript: they carry tool_result blocks and no
                        # text. They must NOT count as human turns — otherwise
                        # the count tracks tool-call volume, not conversation
                        # length (a tool-heavy session inflates ~10x).
                        if isinstance(content, list) and any(
                            isinstance(b, dict) and b.get("type") == "tool_result"
                            for b in content
                        ):
                            continue
                        text = (
                            content
                            if isinstance(content, str)
                            else _text_from_message_content(content)
                        )
                        # Require genuine text (mirrors the Cursor branch's
                        # `if text` guard) so empty-content user entries are
                        # never tallied.
                        if text.strip() and "<command-message>" not in text:
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

    if str(data.get("stop_hook_active", "")).lower() in ("true", "1", "yes"):
        print("{}")
        return

    raw_sid = data.get("conversation_id") or data.get("session_id") or "unknown"
    session_id = _sanitize(str(raw_sid))
    tp = data.get("transcript_path")
    transcript = tp if isinstance(tp, str) else ""

    count = _count_human_messages(transcript)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    marker = STATE_DIR / f"{session_id}_last_checkpoint"
    # Fall back to pre-merge marker names (verbatim_stop.py wrote
    # _verbatim_last_save; stop.py wrote _last_note_review) so a session
    # that started before the checkpoint merge doesn't see its entire
    # transcript as "since last checkpoint" on the first new-hook fire.
    # max() picks the most recent saved point across any markers present.
    legacy_markers = (
        STATE_DIR / f"{session_id}_verbatim_last_save",
        STATE_DIR / f"{session_id}_last_note_review",
    )
    last = 0
    for path in (marker, *legacy_markers):
        if path.is_file():
            try:
                last = max(last, int(path.read_text().strip()))
            except (ValueError, OSError):
                pass

    since = count - last
    _log(f"session={session_id} count={count} last={last} since={since}")

    if since >= CHECKPOINT_INTERVAL and count > 0:
        marker.write_text(str(count))
        window_start = last + 1
        window_end = count
        reason = _build_reason(window_start, window_end, since)
        _log(f"triggering checkpoint at turn {count} (window {window_start}-{window_end})")
        if args.harness == "cursor":
            print(json.dumps({"followup_message": reason}))
        else:
            print(json.dumps({"decision": "block", "reason": reason}))
    else:
        print("{}")


if __name__ == "__main__":
    main()
