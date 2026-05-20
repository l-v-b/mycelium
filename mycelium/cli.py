"""Mycelium CLI — install, verify, reindex."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOOKS_DIR  = Path(__file__).parent / "hooks"
DEPLOY_DIR = Path.home() / ".mycelium"


SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def _build_hooks_snippet(hooks_dir: Path) -> dict:
    return {
        "UserPromptSubmit": [{"matcher": "", "hooks": [
            {"type": "command", "command": f"python3 {hooks_dir}/userpromptsubmit.py --harness claude-code || true"},
        ]}],
        "Stop": [{"matcher": "", "hooks": [
            {"type": "command", "command": f"python3 {hooks_dir}/mempalace_stop.py --harness claude-code || true"},
            {"type": "command", "command": f"python3 {hooks_dir}/stop.py --harness claude-code || true"},
        ]}],
        "PreCompact": [{"matcher": "", "hooks": [
            {"type": "command", "command": f"python3 {hooks_dir}/mempalace_stop.py --harness claude-code || true"},
            {"type": "command", "command": f"python3 {hooks_dir}/stop.py --harness claude-code || true"},
        ]}],
    }


def cmd_install(args: list[str]) -> None:
    """Deploy hooks to ~/.mycelium/hooks/.

    By default prints a settings.json snippet for manual paste. Pass
    --auto-hooks to write our entries directly into ~/.claude/settings.json
    (replaces UserPromptSubmit/Stop/PreCompact, preserves everything else).
    """
    auto = "--auto-hooks" in args

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

    snippet = _build_hooks_snippet(hooks_out)

    if not auto:
        print("\nAdd to ~/.claude/settings.json hooks section:")
        print(json.dumps({"hooks": snippet}, indent=2))
        print("\n(Use 'mycelium install --auto-hooks' to write this directly.)")
        return

    # --auto-hooks: merge into existing settings.json
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    settings: dict = {}
    if SETTINGS_PATH.exists():
        try:
            settings = json.loads(SETTINGS_PATH.read_text())
        except json.JSONDecodeError as e:
            print(f"ERROR: {SETTINGS_PATH} is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        backup = SETTINGS_PATH.with_suffix(".json.mycelium-bak")
        shutil.copy2(SETTINGS_PATH, backup)
        print(f"  Backed up existing settings → {backup}")

    existing_hooks = settings.get("hooks", {}) or {}
    for key in ("UserPromptSubmit", "Stop", "PreCompact"):
        existing_hooks[key] = snippet[key]
    settings["hooks"] = existing_hooks

    SETTINGS_PATH.write_text(json.dumps(settings, indent=2))
    print(f"\n  Wrote hooks → {SETTINGS_PATH}")
    print("  (UserPromptSubmit / Stop / PreCompact replaced; other entries preserved.)")


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
        print("  install              Deploy hooks to ~/.mycelium/ + print/write settings.json snippet")
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
