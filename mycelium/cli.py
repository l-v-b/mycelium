"""Mycelium CLI — install, verify, reindex."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

HOOKS_DIR  = Path(__file__).parent / "hooks"
DEPLOY_DIR = Path.home() / ".mycelium"

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CURSOR_HOOKS_PATH    = Path.home() / ".cursor" / "hooks.json"

# Marker used to identify mycelium-owned hook entries during append-and-dedup.
# Anything containing this substring in its command string is treated as ours
# and replaced on re-run; entries without it survive untouched.
HOOK_MARKER = ".mycelium/hooks/"

SUPPORTED_CLIENTS = ("claude", "cursor")


# ---------------------------------------------------------------------------
# Hook-snippet builders (per client format)
# ---------------------------------------------------------------------------

def _claude_hooks_snippet(hooks_dir: Path) -> dict:
    """Claude Code's settings.json `hooks` section shape.

    Schema: {<EventName>: [{matcher, hooks: [{type: command, command}, ...]}, ...]}
    """
    return {
        "UserPromptSubmit": [{"matcher": "", "hooks": [
            {"type": "command", "command": f"python3 {hooks_dir}/userpromptsubmit.py --harness claude-code || true"},
        ]}],
        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": f"python3 {hooks_dir}/verbatim_stop.py --harness claude-code || true"},
            {"type": "command", "command": f"python3 {hooks_dir}/stop.py --harness claude-code || true"},
        ]}],
        "PreCompact": [{"matcher": "", "hooks": [
            {"type": "command", "command": f"python3 {hooks_dir}/verbatim_stop.py --harness claude-code || true"},
            {"type": "command", "command": f"python3 {hooks_dir}/stop.py --harness claude-code || true"},
        ]}],
    }


def _cursor_hooks_snippet(hooks_dir: Path) -> dict:
    """Cursor's hooks.json shape.

    Cursor uses a flat list per event — no `matcher`, no nested `hooks` array.
    Event names also differ from Claude Code:
        beforeSubmitPrompt  ≈  Claude Code UserPromptSubmit
        stop                ≈  Claude Code Stop
    (Cursor has no PreCompact equivalent.)

    Cursor's response format also differs (followup_message vs decision/reason)
    but the hook scripts already detect `--harness cursor` and adjust.
    """
    return {
        "beforeSubmitPrompt": [
            {"command": f"python3 {hooks_dir}/userpromptsubmit.py --harness cursor || true"},
        ],
        "stop": [
            {"command": f"python3 {hooks_dir}/verbatim_stop.py --harness cursor || true"},
            {"command": f"python3 {hooks_dir}/stop.py --harness cursor || true"},
        ],
    }


# ---------------------------------------------------------------------------
# Append-and-dedup merge logic (idempotent re-run)
# ---------------------------------------------------------------------------

def _merge_claude_settings(existing: dict, new_hooks: dict) -> dict:
    """Merge mycelium's hook entries into ~/.claude/settings.json.

    Only the three event keys in `new_hooks` are touched — other top-level keys
    (env, theme, mcpServers, ...) and other hook events (PreToolUse, etc.) are
    preserved. Within each touched event, pre-existing mycelium-owned entries
    (detected by the HOOK_MARKER substring in their `command`) are removed and
    the new entries appended — so re-runs are idempotent AND don't clobber a
    user's own non-mycelium hooks at the same event.
    """
    out = dict(existing)
    out_hooks = dict(out.get("hooks") or {})
    for event_name, new_entries in new_hooks.items():
        existing_entries = list(out_hooks.get(event_name) or [])
        cleaned = [
            entry for entry in existing_entries
            if not any(HOOK_MARKER in h.get("command", "") for h in entry.get("hooks", []))
        ]
        out_hooks[event_name] = cleaned + list(new_entries)
    out["hooks"] = out_hooks
    return out


def _merge_cursor_hooks(existing: dict, new_hooks: dict) -> dict:
    """Merge mycelium's hook entries into ~/.cursor/hooks.json.

    Schema: `{"version": 1, "hooks": {"beforeSubmitPrompt": [{command}, ...], "stop": [...]}}`
    — same nested shape as Claude but per-entry is just `{"command": "..."}`
    with no matcher/hooks-array wrapper.

    Other top-level keys (`version`, etc.) and other hook events are preserved.
    Within each touched event, pre-existing mycelium-owned entries (matching
    HOOK_MARKER in command) are removed and the new ones appended.
    """
    out = dict(existing)
    out_hooks = dict(out.get("hooks") or {})
    for event_name, new_entries in new_hooks.items():
        existing_entries = list(out_hooks.get(event_name) or [])
        cleaned = [
            entry for entry in existing_entries
            if HOOK_MARKER not in entry.get("command", "")
        ]
        out_hooks[event_name] = cleaned + list(new_entries)
    out["hooks"] = out_hooks
    return out


# ---------------------------------------------------------------------------
# Client resolution
# ---------------------------------------------------------------------------

def _autodetect_clients() -> set[str]:
    """Return the set of clients whose config dir exists on this host."""
    detected: set[str] = set()
    if (Path.home() / ".claude").is_dir():
        detected.add("claude")
    if (Path.home() / ".cursor").is_dir():
        detected.add("cursor")
    return detected


def _resolve_clients(arg: str | None) -> set[str]:
    """Parse --clients=a,b argument or auto-detect.

    Raises SystemExit on unknown client names.
    """
    if arg is None:
        return _autodetect_clients()
    requested = {c.strip().lower() for c in arg.split(",") if c.strip()}
    unknown = requested - set(SUPPORTED_CLIENTS)
    if unknown:
        print(f"ERROR: unknown client(s): {', '.join(sorted(unknown))}", file=sys.stderr)
        print(f"Supported: {', '.join(SUPPORTED_CLIENTS)}", file=sys.stderr)
        sys.exit(1)
    return requested


# ---------------------------------------------------------------------------
# Per-client install routines
# ---------------------------------------------------------------------------

def _backup_and_load(path: Path) -> dict:
    """Read JSON at `path`, taking a `.mycelium-bak` snapshot if it exists.

    Returns empty dict if the file is missing.
    """
    if not path.exists():
        return {}
    try:
        existing = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: {path} is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    backup = path.with_suffix(path.suffix + ".mycelium-bak")
    shutil.copy2(path, backup)
    print(f"  Backed up existing config → {backup}")
    return existing if isinstance(existing, dict) else {}


def _install_for_claude(hooks_dir: Path, auto: bool, dry_run: bool) -> None:
    snippet = _claude_hooks_snippet(hooks_dir)
    if not auto:
        print("\n[claude] Add to ~/.claude/settings.json hooks section:")
        print(json.dumps({"hooks": snippet}, indent=2))
        print("(Re-run with --auto-hooks to write this directly.)")
        return

    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _backup_and_load(CLAUDE_SETTINGS_PATH)
    merged = _merge_claude_settings(existing, snippet)

    if dry_run:
        print(f"\n[claude] [DRY-RUN] Would write to {CLAUDE_SETTINGS_PATH}:")
        print(json.dumps(merged, indent=2))
        return

    CLAUDE_SETTINGS_PATH.write_text(json.dumps(merged, indent=2))
    print(f"\n[claude] Wrote hooks → {CLAUDE_SETTINGS_PATH}")
    print("[claude] (mycelium-owned entries replaced; other hooks preserved.)")


def _install_for_cursor(hooks_dir: Path, auto: bool, dry_run: bool) -> None:
    snippet = _cursor_hooks_snippet(hooks_dir)
    if not auto:
        print("\n[cursor] Add to ~/.cursor/hooks.json:")
        print(json.dumps(snippet, indent=2))
        print("(Re-run with --auto-hooks to write this directly.)")
        return

    CURSOR_HOOKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _backup_and_load(CURSOR_HOOKS_PATH)
    merged = _merge_cursor_hooks(existing, snippet)

    if dry_run:
        print(f"\n[cursor] [DRY-RUN] Would write to {CURSOR_HOOKS_PATH}:")
        print(json.dumps(merged, indent=2))
        return

    CURSOR_HOOKS_PATH.write_text(json.dumps(merged, indent=2))
    print(f"\n[cursor] Wrote hooks → {CURSOR_HOOKS_PATH}")
    print("[cursor] (mycelium-owned entries replaced; other hooks preserved.)")


# ---------------------------------------------------------------------------
# install command
# ---------------------------------------------------------------------------

def _parse_clients_flag(args: list[str]) -> str | None:
    """Lightweight extraction of --clients value (supports both --clients X
    and --clients=X). Returns None if absent (caller auto-detects)."""
    for i, a in enumerate(args):
        if a == "--clients" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith("--clients="):
            return a.split("=", 1)[1]
    return None


def cmd_install(args: list[str]) -> None:
    """Deploy hooks to ~/.mycelium/hooks/ and (with --auto-hooks) wire them
    into one or more client config files.

    Usage:
      mycelium install [--clients claude,cursor] [--auto-hooks] [--dry-run]

    Without --clients: autodetect which clients are installed (Claude Code at
    ~/.claude/, Cursor at ~/.cursor/) and target each one.

    Without --auto-hooks: print the snippet for each target client; user pastes
    it themselves. With --auto-hooks: write the entries directly using an
    append-and-dedup-by-marker merge — re-runs are idempotent AND other tools'
    hooks at the same events survive untouched.

    --dry-run: print the resulting config without writing.
    """
    auto    = "--auto-hooks" in args
    dry_run = "--dry-run"    in args
    clients = _resolve_clients(_parse_clients_flag(args))

    if not clients:
        print("ERROR: No clients to install for. Pass --clients claude,cursor", file=sys.stderr)
        print("or install Claude Code / Cursor first (auto-detect looks for ~/.claude/, ~/.cursor/).", file=sys.stderr)
        sys.exit(1)

    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    hooks_out = DEPLOY_DIR / "hooks"
    hooks_out.mkdir(exist_ok=True)
    for hook in ("userpromptsubmit.py", "stop.py", "verbatim_stop.py"):
        src = HOOKS_DIR / hook
        dst = hooks_out / hook
        if src.exists():
            shutil.copy2(src, dst)
            print(f"  Installed: {dst}")
        else:
            print(f"  WARNING: {src} not found in package")

    print(f"\nTarget clients: {', '.join(sorted(clients))}")
    if "claude" in clients:
        _install_for_claude(hooks_out, auto, dry_run)
    if "cursor" in clients:
        _install_for_cursor(hooks_out, auto, dry_run)


def cmd_verify(args: list[str]) -> None:
    """Quick health check: imports, vault dirs, index counts."""
    from mycelium.config import VAULT_DIR, CHROMA_DIR
    print(f"VAULT_DIR:  {VAULT_DIR}")
    print(f"CHROMA_DIR: {CHROMA_DIR}")

    try:
        from mycelium.chroma import notes_collection, drawers_collection, links_collection
        print(f"notes:   {notes_collection().count()} indexed")
        print(f"drawers: {drawers_collection().count()} indexed")
        print(f"links:   {links_collection().count()} indexed")
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print("OK")


def cmd_reindex(args: list[str]) -> None:
    """Sync ChromaDB index with vault/ markdown files (upsert + prune orphans).

    Respects MYCELIUM_REINDEX_THREADS env var. Set to a low value (e.g. 2)
    when running on a shared host so ONNX embedding compute doesn't starve
    other containers. Must be set BEFORE any chroma/onnx imports happen.
    """
    threads = os.environ.get("MYCELIUM_REINDEX_THREADS")
    if threads:
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ.setdefault(var, threads)
        print(f"Limiting ONNX threads to {threads}")

    from mycelium.vault import reindex_notes, reindex_drawers, reindex_links
    n_up, n_orph = reindex_notes()
    d_up, d_orph = reindex_drawers()
    l_up, l_orph = reindex_links()
    print(f"Notes:   {n_up} upserted, {n_orph} orphans pruned")
    print(f"Drawers: {d_up} upserted, {d_orph} orphans pruned")
    print(f"Links:   {l_up} upserted, {l_orph} orphans pruned")


def cmd_regenerate_closets(args: list[str]) -> None:
    """Rebuild closets collection from current drawers (run after bulk imports
    or when search quality drifts). Closets group drawers by (wing, room)
    and provide a ranking boost when the cluster matches a query.
    """
    from mycelium.vault import regenerate_closets
    c_up, c_orph = regenerate_closets()
    print(f"Closets: {c_up} upserted, {c_orph} orphans pruned")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: mycelium <command> [args]")
        print("\nCommands:")
        print("  install              Deploy hooks + (with --auto-hooks) wire into Claude Code / Cursor")
        print("                       Flags: --clients claude,cursor (default: auto-detect)")
        print("                              --auto-hooks (write directly, append-and-dedup-by-marker)")
        print("                              --dry-run    (print the would-be config, no writes)")
        print("  verify               Health check: imports, vault, index counts")
        print("  reindex              Sync ChromaDB (notes, drawers, links) with vault/ markdown files")
        print("  regenerate-closets   Rebuild closet topical-cluster index from current drawers")
        return

    cmd, rest = args[0], args[1:]
    dispatch = {
        "install":             cmd_install,
        "verify":              cmd_verify,
        "reindex":             cmd_reindex,
        "regenerate-closets":  cmd_regenerate_closets,
    }
    fn = dispatch.get(cmd)
    if fn:
        fn(rest)
    else:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        sys.exit(1)
