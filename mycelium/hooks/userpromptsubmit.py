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
  "contextforge_url":    "https://<your-contextforge-host>",
  "contextforge_token":  "<jwt without 'Bearer ' prefix>",
  "mempalace_server_id": "<your-aggregated-server-id>",
  "n_notes":             60,
  "n_drawers":           60,
  "n_links":             20,
  "max_state_ids":       2000
}

State: ~/.mycelium/state/{session_id}_surfaced.json
  {
    "note_ids":      [...],
    "drawer_ids":    [...],
    "link_ids":      [...],
    "recent_yields": [int, int, ...],  # rolling window of last N injection counts
  }

Adaptive sizing: when the rolling-mean of recent yields drops below a
threshold (because dedup is starving the agent of net-new context), the
NEXT call widens its search radius (2× fetch counts + 0.10 max_distance)
to find relevant items beyond the current top-N cutoff.
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

# Adaptive sizing tunables — widen the next fetch when the rolling-mean of
# recent yields drops below ADAPTIVE_TRIGGER_MEAN over a window of
# ADAPTIVE_WINDOW recent calls. Widen multiplies fetch counts by
# ADAPTIVE_WIDEN_FACTOR and bumps max_distance by ADAPTIVE_WIDEN_DISTANCE.
# Mean recovery naturally resets the behaviour — no explicit reset needed.
ADAPTIVE_WINDOW          = 5
ADAPTIVE_TRIGGER_MEAN    = 2.0
ADAPTIVE_WIDEN_FACTOR    = 2
ADAPTIVE_WIDEN_DISTANCE  = 0.10
DEFAULT_MAX_DISTANCE     = 0.75


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
        return {"note_ids": [], "drawer_ids": [], "link_ids": [], "recent_yields": []}
    try:
        with open(p) as f:
            d = json.load(f)
        # Normalise — accept legacy formats / partial state files
        return {
            "note_ids":      list(d.get("note_ids", [])),
            "drawer_ids":    list(d.get("drawer_ids", [])),
            "link_ids":      list(d.get("link_ids", [])),
            "recent_yields": list(d.get("recent_yields", [])),
        }
    except (json.JSONDecodeError, OSError):
        return {"note_ids": [], "drawer_ids": [], "link_ids": [], "recent_yields": []}


def _save_surfaced(session_id: str, state: dict, max_ids: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Cap each list to prevent runaway state file growth on long sessions.
    # FIFO trim — oldest IDs drop out first, so very-stale items could
    # re-surface after the cap is hit. Acceptable for personal use.
    trimmed: dict = {}
    for k, v in state.items():
        if k == "recent_yields":
            # Rolling window of last N injection counts (small, bounded).
            trimmed[k] = list(v[-ADAPTIVE_WINDOW:])
        else:
            trimmed[k] = list(v[-max_ids:])
    try:
        with open(_state_path(session_id), "w") as f:
            json.dump(trimmed, f)
    except OSError as e:
        _log(f"Failed to write state: {e}")


def _adaptive_params(
    config: dict, recent_yields: list[int]
) -> tuple[int, int, int, float, bool]:
    """Return (n_notes, n_drawers, n_links, max_distance, widened).

    Widens the next fetch when the rolling-mean of `recent_yields` over the
    last ADAPTIVE_WINDOW calls drops below ADAPTIVE_TRIGGER_MEAN — typically
    because dedup is starving the agent of net-new context. Multiplies fetch
    counts and bumps max_distance for the next call only; the wider-yield
    feedback naturally raises the rolling mean and brings the next call back
    to baseline.
    """
    n_notes   = int(config.get("n_notes", 60))
    n_drawers = int(config.get("n_drawers", 60))
    n_links   = int(config.get("n_links", 20))
    max_dist  = float(config.get("max_distance", DEFAULT_MAX_DISTANCE))

    if len(recent_yields) >= ADAPTIVE_WINDOW:
        mean_yield = sum(recent_yields[-ADAPTIVE_WINDOW:]) / ADAPTIVE_WINDOW
        if mean_yield < ADAPTIVE_TRIGGER_MEAN:
            return (
                n_notes   * ADAPTIVE_WIDEN_FACTOR,
                n_drawers * ADAPTIVE_WIDEN_FACTOR,
                n_links   * ADAPTIVE_WIDEN_FACTOR,
                min(max_dist + ADAPTIVE_WIDEN_DISTANCE, 0.99),
                True,
            )
    return n_notes, n_drawers, n_links, max_dist, False


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


def _fetch_titles(
    config: dict,
    query: str,
    n_notes: int,
    n_drawers: int,
    n_links: int,
    max_distance: float,
) -> dict:
    base       = config["contextforge_url"].rstrip("/")
    server_id  = config["mempalace_server_id"]
    token      = config["contextforge_token"].removeprefix("Bearer ").strip()

    payload = json.dumps({
        "jsonrpc": "2.0",
        "method":  "tools/call",
        "params": {
            "name": TOOL_NAME,
            "arguments": {
                "query":         query[:250],
                "n_notes":       n_notes,
                "n_drawers":     n_drawers,
                "n_links":       n_links,
                "max_distance":  max_distance,
                "_caller":       "hook",
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
        "note_ids":      [n["note_id"] for n in new_notes],
        "drawer_ids":    [d["drawer_id"] for d in new_drawers],
        "link_ids":      [l["link_id"] for l in new_links],
        # Human-readable summaries for hook.log — titles for notes,
        # wing/room for drawers, source--rel-->target for links.
        "note_titles":   [n.get("title", "Untitled") for n in new_notes],
        "drawer_labels": [f"{d.get('wing','?')}/{d.get('room','?')}" for d in new_drawers],
        "link_labels":   [
            f"{l.get('source',{}).get('label','?')} --[{l.get('relation_type','?')}]--> {l.get('target',{}).get('label','?')}"
            for l in new_links
        ],
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
        surfaced = _load_surfaced(session_id)
        n_notes_arg, n_drawers_arg, n_links_arg, max_dist_arg, widened = _adaptive_params(
            config, surfaced.get("recent_yields", [])
        )

        titles = _fetch_titles(
            config, effective_query,
            n_notes_arg, n_drawers_arg, n_links_arg, max_dist_arg,
        )
        formatted, just_surfaced = _dedup_and_format(titles, surfaced)

        # Record this call's yield (count of net-new items across all sources)
        # in the rolling window so the next call's adaptive params know whether
        # to widen further or settle.
        n_notes   = len(just_surfaced["note_ids"])
        n_drawers = len(just_surfaced["drawer_ids"])
        n_links   = len(just_surfaced["link_ids"])
        yield_count = n_notes + n_drawers + n_links
        new_recent = list(surfaced.get("recent_yields", [])) + [yield_count]

        # Always update state with what we surfaced + the new yield reading —
        # even when injection is empty, the act of querying counts (the IDs
        # were already surfaced to the agent at some prior point).
        # Note: just_surfaced also carries human-readable titles/labels for
        # logging; those aren't persisted into state, only IDs are.
        merged = {
            "note_ids":      surfaced["note_ids"]   + just_surfaced["note_ids"],
            "drawer_ids":    surfaced["drawer_ids"] + just_surfaced["drawer_ids"],
            "link_ids":      surfaced["link_ids"]   + just_surfaced["link_ids"],
            "recent_yields": new_recent,
        }
        max_ids = int(config.get("max_state_ids", 2000))
        _save_surfaced(session_id, merged, max_ids)

        if not formatted:
            # Quiet "all seen" case — log but don't inject. Most common
            # state for prompts that continue an existing topic.
            n_seen = sum(len(surfaced[k]) for k in ("note_ids", "drawer_ids", "link_ids"))
            _log(f"[{session_id}] all relevant titles already surfaced (state has {n_seen} ids){' [widened]' if widened else ''}")
            _out({})
            return

        _log(
            f"[{session_id}] {prompt[:60]!r} -> "
            f"+{n_notes}n / +{n_drawers}d / +{n_links}l "
            f"({len(formatted)} chars)"
            f"{' [widened: ' + str(n_notes_arg) + '/' + str(n_drawers_arg) + '/' + str(n_links_arg) + ' @' + str(round(max_dist_arg, 2)) + ']' if widened else ''}"
        )
        # Inline the surfaced titles/labels so a tail of hook.log shows
        # what the agent actually got injected, not just counts.
        for title in just_surfaced.get("note_titles", []):
            _log(f"  [note]   {title}")
        for label in just_surfaced.get("drawer_labels", []):
            _log(f"  [drawer] {label}")
        for label in just_surfaced.get("link_labels", []):
            _log(f"  [link]   {label}")
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
