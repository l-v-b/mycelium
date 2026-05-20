"""Mycelium CLI — install, verify, reindex."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HOOKS_DIR  = Path(__file__).parent / "hooks"
DEPLOY_DIR = Path.home() / ".mycelium"


def cmd_install(args: list[str]) -> None:
    """Deploy hooks to ~/.mycelium/hooks/ and print settings.json snippet."""
    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    hooks_out = DEPLOY_DIR / "hooks"
    hooks_out.mkdir(exist_ok=True)

    for hook in ["userpromptsubmit.py", "stop.py", "mempalace_stop.py"]:
        src = HOOKS_DIR / hook
        dst = hooks_out / hook
        if src.exists():
            shutil.copy2(src, dst)
            print(f"  Installed: {dst}")
        else:
            print(f"  WARNING: {src} not found in package")

    print("\nAdd to ~/.claude/settings.json hooks section:")
    snippet = {
        "UserPromptSubmit": [{"matcher": "", "hooks": [
            {"type": "command", "command": f"python3 {hooks_out}/userpromptsubmit.py --harness claude-code || true"},
        ]}],
        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": f"python3 {hooks_out}/mempalace_stop.py --harness claude-code || true"},
            {"type": "command", "command": f"python3 {hooks_out}/stop.py --harness claude-code || true"},
        ]}],
        "PreCompact": [{"matcher": "", "hooks": [
            {"type": "command", "command": f"python3 {hooks_out}/mempalace_stop.py --harness claude-code || true"},
            {"type": "command", "command": f"python3 {hooks_out}/stop.py --harness claude-code || true"},
        ]}],
    }
    print(json.dumps({"hooks": snippet}, indent=2))


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
    """Rebuild ChromaDB index from vault/ markdown files."""
    from mycelium.vault import reindex_notes, reindex_drawers
    n = reindex_notes()
    d = reindex_drawers()
    print(f"Reindexed: {n} notes, {d} drawers")


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: mycelium <command> [args]")
        print("\nCommands:")
        print("  install    Deploy hooks to ~/.mycelium/ and print settings.json snippet")
        print("  verify     Health check: imports, vault, index counts")
        print("  reindex    Rebuild ChromaDB from vault/ markdown files")
        return

    cmd, rest = args[0], args[1:]
    dispatch = {
        "install": cmd_install,
        "verify":  cmd_verify,
        "reindex": cmd_reindex,
    }
    fn = dispatch.get(cmd)
    if fn:
        fn(rest)
    else:
        print(f"Unknown command: {cmd!r}", file=sys.stderr)
        sys.exit(1)
