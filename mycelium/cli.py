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
SKILLS_DIR = Path(__file__).parent / "skills"
DEPLOY_DIR = Path.home() / ".mycelium"

# Where in-package skills land after `mycelium install`. Adding this path to
# the client's skillsDirectories makes the skills discoverable.
DEPLOYED_MYCELIUM_SKILLS_DIR = DEPLOY_DIR / "skills" / "mycelium"

# Where the personal-skills repo is cloned by `mycelium skills sync`.
DEPLOYED_PERSONAL_SKILLS_DIR = DEPLOY_DIR / "skills" / "personal"

CONFIG_PATH          = DEPLOY_DIR / "config.json"
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"
CLAUDE_MCP_PATH      = Path.home() / ".claude.json"
CURSOR_HOOKS_PATH    = Path.home() / ".cursor" / "hooks.json"

# Default name of the MCP server entry written by `install --add-mcp-server`.
# Configurable via the --mcp-name flag. Users going through a ContextForge
# gateway typically want "contextforge"; users connecting directly to a
# local mycelium SSE endpoint typically want "mycelium".
DEFAULT_MCP_NAME = "mycelium"

# Vault layout — mirrors mycelium/config.py VAULT_DIR subdirs.
VAULT_SUBDIRS = ("notes", "drawers", "diary", "concepts", "links")

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


def _deploy_skills(skills_src: Path, skills_dst: Path) -> int:
    """Copy in-package skill directories from `skills_src` to `skills_dst`.

    Each top-level subdirectory of `skills_src` is a skill. `README.md`
    at the top level is informational only and not copied (it documents
    the convention for developers, not Claude clients).

    Returns the number of skills deployed (excluding the README).
    """
    if not skills_src.is_dir():
        return 0
    skills_dst.mkdir(parents=True, exist_ok=True)
    deployed = 0
    for entry in sorted(skills_src.iterdir()):
        if entry.name == "README.md":
            continue
        if entry.is_dir():
            target = skills_dst / entry.name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(entry, target)
            print(f"  Installed skill: {target}")
            deployed += 1
    return deployed


def _merge_claude_skills_dirs(existing: dict, path: str) -> dict:
    """Idempotently append `path` to settings.skillsDirectories.

    Preserves any other entries in the list (e.g. user-managed skill dirs
    or other tools' skills). Re-running is a no-op once the path is present.
    """
    out = dict(existing)
    dirs = list(out.get("skillsDirectories") or [])
    if path not in dirs:
        dirs.append(path)
    out["skillsDirectories"] = dirs
    return out


def _install_for_claude(hooks_dir: Path, skills_dirs: list[Path], auto: bool, dry_run: bool) -> None:
    snippet = _claude_hooks_snippet(hooks_dir)
    skills_snippet = (
        {"skillsDirectories": [str(d) for d in skills_dirs]} if skills_dirs else {}
    )

    if not auto:
        print("\n[claude] Add to ~/.claude/settings.json hooks section:")
        print(json.dumps({"hooks": snippet}, indent=2))
        if skills_snippet:
            print("\n[claude] Also append to ~/.claude/settings.json skillsDirectories:")
            print(json.dumps(skills_snippet, indent=2))
        print("(Re-run with --auto-hooks to write this directly.)")
        return

    CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _backup_and_load(CLAUDE_SETTINGS_PATH)
    merged = _merge_claude_settings(existing, snippet)
    for d in skills_dirs:
        merged = _merge_claude_skills_dirs(merged, str(d))

    if dry_run:
        print(f"\n[claude] [DRY-RUN] Would write to {CLAUDE_SETTINGS_PATH}:")
        print(json.dumps(merged, indent=2))
        return

    CLAUDE_SETTINGS_PATH.write_text(json.dumps(merged, indent=2))
    print(f"\n[claude] Wrote hooks → {CLAUDE_SETTINGS_PATH}")
    print("[claude] (mycelium-owned entries replaced; other hooks preserved.)")
    for d in skills_dirs:
        print(f"[claude] skillsDirectories includes: {d}")


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
# Config + MCP-server-entry helpers
# ---------------------------------------------------------------------------

def _read_config() -> dict:
    """Read ~/.mycelium/config.json, returning empty dict if missing/invalid."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def _prompt(label: str, default: str = "") -> str:
    """Interactive prompt with default shown in brackets."""
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{label}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.", file=sys.stderr)
        sys.exit(1)
    return raw or default


def _claude_mcp_entry(url: str, token: str, server_id: str) -> dict:
    """Build the Claude Code mcpServers entry for a mycelium / contextforge endpoint.

    URL shape:
      - With server_id: <url>/servers/<server_id>/mcp   (ContextForge-gateway pattern)
      - Without:        <url>                            (direct mycelium SSE pattern)
    """
    full_url = f"{url.rstrip('/')}/servers/{server_id}/mcp" if server_id else url
    entry: dict = {"type": "http", "url": full_url}
    if token:
        entry["headers"] = {"Authorization": f"Bearer {token}"}
    return entry


def _merge_claude_mcp(existing: dict, name: str, entry: dict) -> dict:
    """Merge a single mcpServers entry into ~/.claude.json.

    Preserves all other top-level keys (env, theme, projects, …) and other
    mcpServers entries. Idempotent: same name re-writes the entry in place.
    """
    out = dict(existing)
    servers = dict(out.get("mcpServers") or {})
    servers[name] = entry
    out["mcpServers"] = servers
    return out


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


def _parse_named_flag(args: list[str], name: str) -> str | None:
    """Generic --foo X / --foo=X extractor."""
    for i, a in enumerate(args):
        if a == f"--{name}" and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None


def _install_mcp_server_for_claude(name: str, entry: dict, dry_run: bool) -> None:
    """Add/update an mcpServers entry in ~/.claude.json.

    Idempotent — re-running with the same name replaces the entry in place.
    All other top-level keys and other mcpServers entries survive untouched.
    """
    CLAUDE_MCP_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = _backup_and_load(CLAUDE_MCP_PATH)
    merged = _merge_claude_mcp(existing, name, entry)

    if dry_run:
        print(f"\n[claude/mcp] [DRY-RUN] Would write to {CLAUDE_MCP_PATH}:")
        print(json.dumps({"mcpServers": {name: entry}}, indent=2))
        return

    CLAUDE_MCP_PATH.write_text(json.dumps(merged, indent=2))
    print(f"\n[claude/mcp] Wrote mcpServers entry {name!r} → {CLAUDE_MCP_PATH}")


def cmd_install(args: list[str]) -> None:
    """Deploy hooks to ~/.mycelium/hooks/ and (with --auto-hooks) wire them
    into one or more client config files. Optionally also register the MCP
    server itself in the client's mcpServers config.

    Usage:
      mycelium install [--clients claude,cursor]
                       [--auto-hooks]
                       [--add-mcp-server] [--mcp-name NAME]
                       [--dry-run]

    Without --clients: autodetect which clients are installed (Claude Code at
    ~/.claude/, Cursor at ~/.cursor/) and target each one.

    Without --auto-hooks: print the hooks snippet for each target client; user
    pastes it themselves. With --auto-hooks: write directly using an
    append-and-dedup-by-marker merge — re-runs are idempotent AND other tools'
    hooks at the same events survive untouched.

    --add-mcp-server: also write an mcpServers entry to ~/.claude.json based
    on ~/.mycelium/config.json (run `mycelium init` first to create that).
    Currently Claude-only; Cursor's MCP config will be added when needed.

    --mcp-name NAME: name of the mcpServers entry (default: "mycelium").

    --dry-run: print the resulting config without writing.
    """
    auto           = "--auto-hooks"     in args
    dry_run        = "--dry-run"        in args
    add_mcp_server = "--add-mcp-server" in args
    mcp_name       = _parse_named_flag(args, "mcp-name") or DEFAULT_MCP_NAME
    skills_repo    = _parse_named_flag(args, "skills-repo")
    clients        = _resolve_clients(_parse_clients_flag(args))

    # If --skills-repo URL was passed, persist into ~/.mycelium/config.json
    # so future `mycelium skills sync` calls find it automatically.
    if skills_repo is not None:
        DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
        cfg = _read_config()
        cfg["personal_skills_repo"] = skills_repo
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        print(f"  Persisted personal_skills_repo={skills_repo} → {CONFIG_PATH}")

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

    skills_count = _deploy_skills(SKILLS_DIR, DEPLOYED_MYCELIUM_SKILLS_DIR)
    skills_dirs: list[Path] = []
    if skills_count > 0:
        skills_dirs.append(DEPLOYED_MYCELIUM_SKILLS_DIR)
    # If `mycelium skills sync` has populated the personal-skills clone,
    # include it in skillsDirectories too.
    if (DEPLOYED_PERSONAL_SKILLS_DIR / ".git").is_dir():
        skills_dirs.append(DEPLOYED_PERSONAL_SKILLS_DIR)

    print(f"\nTarget clients: {', '.join(sorted(clients))}")
    if "claude" in clients:
        _install_for_claude(hooks_out, skills_dirs, auto, dry_run)
    if "cursor" in clients:
        _install_for_cursor(hooks_out, auto, dry_run)

    if add_mcp_server:
        cfg = _read_config()
        url       = cfg.get("contextforge_url", "")
        token     = cfg.get("contextforge_token", "")
        server_id = cfg.get("mempalace_server_id", "")
        if not url:
            print("\nERROR: --add-mcp-server requires contextforge_url in ~/.mycelium/config.json.",
                  file=sys.stderr)
            print("       Run `mycelium init` first to create it.", file=sys.stderr)
            sys.exit(1)
        entry = _claude_mcp_entry(url, token, server_id)
        if "claude" in clients:
            _install_mcp_server_for_claude(mcp_name, entry, dry_run)
        if "cursor" in clients:
            print("\n[cursor/mcp] --add-mcp-server: Cursor MCP config not yet supported. "
                  "Edit ~/.cursor/mcp.json manually with the entry shown above.")


def cmd_skills(args: list[str]) -> None:
    """Dispatch for `mycelium skills <subcommand>`.

    Subcommands:
      sync   — clone or pull the personal-skills repo (see cmd_skills_sync)
    """
    if not args or args[0] in ("-h", "--help"):
        print("Usage: mycelium skills <subcommand> [args]")
        print("\nSubcommands:")
        print("  sync     Clone/pull the personal-skills repo to ~/.mycelium/skills/personal/")
        print("           Flags: --repo URL (override config; does not persist)")
        return
    sub, rest = args[0], args[1:]
    if sub == "sync":
        return cmd_skills_sync(rest)
    print(f"Unknown skills subcommand: {sub!r}", file=sys.stderr)
    sys.exit(1)


def cmd_skills_sync(args: list[str]) -> None:
    """Clone or pull the personal-skills repo into ~/.mycelium/skills/personal/.

    Usage:
      mycelium skills sync [--repo URL]

    Reads `personal_skills_repo` from ~/.mycelium/config.json. Override
    for one-shot use with --repo URL (does NOT update config.json — use
    `mycelium install --skills-repo URL` to persist the URL).

    First run: `git clone <URL> ~/.mycelium/skills/personal/`.
    Subsequent runs: `git -C ... pull --ff-only`.

    After syncing, run `mycelium install --auto-hooks` to ensure the
    skills directory is in `~/.claude/settings.json` skillsDirectories.
    """
    cfg = _read_config()
    repo = _parse_named_flag(args, "repo") or cfg.get("personal_skills_repo", "")
    if not repo:
        print("ERROR: No personal-skills repo configured.", file=sys.stderr)
        print("       Run `mycelium install --skills-repo URL` to persist a URL", file=sys.stderr)
        print("       or pass `--repo URL` for one-shot sync.", file=sys.stderr)
        sys.exit(1)

    DEPLOYED_PERSONAL_SKILLS_DIR.parent.mkdir(parents=True, exist_ok=True)
    if (DEPLOYED_PERSONAL_SKILLS_DIR / ".git").is_dir():
        print(f"Pulling latest into {DEPLOYED_PERSONAL_SKILLS_DIR}")
        rc = subprocess.run(
            ["git", "-C", str(DEPLOYED_PERSONAL_SKILLS_DIR), "pull", "--ff-only"],
            check=False,
        ).returncode
    else:
        print(f"Cloning {repo} → {DEPLOYED_PERSONAL_SKILLS_DIR}")
        rc = subprocess.run(
            ["git", "clone", repo, str(DEPLOYED_PERSONAL_SKILLS_DIR)],
            check=False,
        ).returncode

    if rc != 0:
        print(f"\nERROR: git operation failed (exit {rc}).", file=sys.stderr)
        sys.exit(rc)

    skill_count = sum(
        1 for p in DEPLOYED_PERSONAL_SKILLS_DIR.iterdir()
        if p.is_dir() and p.name != ".git" and (p / "SKILL.md").exists()
    ) if DEPLOYED_PERSONAL_SKILLS_DIR.is_dir() else 0
    print(f"\nDone. {skill_count} skill(s) under {DEPLOYED_PERSONAL_SKILLS_DIR}.")
    print(f"Run `mycelium install --auto-hooks` to ensure this path is in skillsDirectories.")


def cmd_init(args: list[str]) -> None:
    """Bootstrap mycelium on a new machine.

    Creates ~/.mycelium/config.json from interactive prompts (or flags) and
    optionally scaffolds the vault directory structure. Re-runnable — existing
    config values become defaults for the prompts.

    Usage:
      mycelium init [--non-interactive]
                    [--url URL] [--token TOKEN] [--server-id ID]
                    [--vault-dir PATH] [--skip-vault] [--force]

    --non-interactive: don't prompt; use supplied flags + existing config.json
        values as defaults. Useful for CI / scripted bootstraps.

    --url, --token, --server-id: override config values from the command line.

    --vault-dir PATH: directory under which the vault subdirs (notes, drawers,
        diary, concepts, links) are created. Defaults to ~/.mycelium/data/vault.

    --skip-vault: don't create the vault directory structure (only write
        config.json — for hook-only deployments connecting to a remote server).

    --force: overwrite existing config.json without re-prompting (still uses
        existing values as the defaults that get re-written).
    """
    non_interactive = "--non-interactive" in args
    skip_vault      = "--skip-vault"      in args
    force           = "--force"           in args

    existing = _read_config()

    url_arg       = _parse_named_flag(args, "url")
    token_arg     = _parse_named_flag(args, "token")
    server_id_arg = _parse_named_flag(args, "server-id")
    vault_arg     = _parse_named_flag(args, "vault-dir")

    default_url       = url_arg       if url_arg       is not None else existing.get("contextforge_url", "")
    default_token     = token_arg     if token_arg     is not None else existing.get("contextforge_token", "")
    default_server    = server_id_arg if server_id_arg is not None else existing.get("mempalace_server_id", "")
    default_vault     = vault_arg     if vault_arg     is not None else str(DEPLOY_DIR / "data" / "vault")

    if non_interactive:
        url, token, server_id, vault_dir = default_url, default_token, default_server, default_vault
    else:
        print("mycelium init — bootstrapping ~/.mycelium/config.json.\n"
              "(Press Enter to accept the default in brackets.)\n")
        url       = _prompt("ContextForge URL (or leave empty for direct connection)", default_url)
        token     = _prompt("ContextForge bearer token (without 'Bearer ' prefix)", default_token)
        server_id = _prompt("Aggregated server ID (gateway mode only)", default_server)
        vault_dir = _prompt("Vault directory", default_vault) if not skip_vault else default_vault

    if CONFIG_PATH.exists() and not force and not non_interactive:
        confirm = _prompt(f"\n{CONFIG_PATH} already exists. Overwrite? [y/N]", "N")
        if confirm.lower() not in ("y", "yes"):
            print("Aborted; config unchanged.", file=sys.stderr)
            sys.exit(1)

    DEPLOY_DIR.mkdir(parents=True, exist_ok=True)
    cfg = dict(existing)
    cfg.update({
        "contextforge_url":    url,
        "contextforge_token":  token,
        "mempalace_server_id": server_id,
    })
    cfg.setdefault("search_limit", 10)
    cfg.setdefault("max_distance", 0.75)
    if CONFIG_PATH.exists():
        backup = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".mycelium-bak")
        shutil.copy2(CONFIG_PATH, backup)
        print(f"  Backed up existing config → {backup}")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"  Wrote {CONFIG_PATH}")

    if not skip_vault:
        vault_root = Path(vault_dir).expanduser()
        vault_root.mkdir(parents=True, exist_ok=True)
        for sub in VAULT_SUBDIRS:
            (vault_root / sub).mkdir(exist_ok=True)
        print(f"  Vault directory: {vault_root}")
        print(f"    subdirs: {', '.join(VAULT_SUBDIRS)}")
    else:
        print("  Skipped vault directory scaffolding (--skip-vault).")

    print("\nDone. Next steps:")
    print("  mycelium install --auto-hooks            # wire hooks into Claude Code / Cursor")
    print("  mycelium install --add-mcp-server        # register MCP server in ~/.claude.json")
    print("  python -m mycelium                       # start the MCP server (if running locally)")


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
        print("  init                 Bootstrap ~/.mycelium/config.json + vault dirs (interactive)")
        print("                       Flags: --non-interactive, --url, --token, --server-id,")
        print("                              --vault-dir, --skip-vault, --force")
        print("  install              Deploy hooks + (with --auto-hooks) wire into Claude Code / Cursor")
        print("                       Flags: --clients claude,cursor (default: auto-detect)")
        print("                              --auto-hooks (write directly, append-and-dedup-by-marker)")
        print("                              --add-mcp-server [--mcp-name NAME]")
        print("                              --skills-repo URL (your personal skills git repo; saved to config.json)")
        print("                              --dry-run    (print the would-be config, no writes)")
        print("  skills sync          Clone/pull personal-skills repo to ~/.mycelium/skills/personal/")
        print("                       Flags: --repo URL (one-shot override)")
        print("  verify               Health check: imports, vault, index counts")
        print("  reindex              Sync ChromaDB (notes, drawers, links) with vault/ markdown files")
        print("  regenerate-closets   Rebuild closet topical-cluster index from current drawers")
        return

    cmd, rest = args[0], args[1:]
    dispatch = {
        "init":                cmd_init,
        "install":             cmd_install,
        "skills":              cmd_skills,
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
