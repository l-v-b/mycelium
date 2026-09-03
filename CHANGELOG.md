# Changelog

All notable changes to mycelium-palace are documented here. The format is based
on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/).

## [2.7.0] — 2026-09-03

### Added
- **Windows support** for the server and CLI.
- **Claude Desktop support** over stdio: `python -m mycelium.stdio`,
  `mycelium serve --transport stdio`, and `mycelium install-desktop` to write
  the per-OS `claude_desktop_config.json` MCP entry (Windows `%APPDATA%`,
  macOS `~/Library/Application Support`, Linux `~/.config`).
- **`/recall` MCP prompt** — deterministic memory injection for clients without
  a hook system (runs `context()` server-side and returns the hits). The manual
  analogue of Claude Code's `UserPromptSubmit` auto-injection.
- **`docker-compose.yml`** — one-file local Postgres + pgvector for a personal
  install; mycelium self-creates its extension and schema on first connect.
- Newcomer-focused README with a Windows/Claude-Desktop quickstart and a
  "how recall reaches the model" section.

### Changed
- `MYCELIUM_DATA_DIR` now defaults to `~/.mycelium/data` (was `/data`, which is
  unwritable on Windows). The container image still sets `/data` explicitly.
- The "call `context()` first" recall guidance now also lives in the `context()`
  tool description, because Claude Desktop ignores the MCP `instructions` field.
- `mycelium.__version__` is single-sourced from installed package metadata
  (was a hardcoded, stale literal).
- The release workflow publishes to **production PyPI** (was TestPyPI).

### Fixed
- All text file I/O uses explicit `encoding="utf-8"`, fixing `UnicodeDecodeError`
  on Windows (cp1252 default) for the non-ASCII content mycelium uses widely.
- Vault git operations are optional — a missing `git` binary no longer raises;
  writes still persist to disk, only versioning/push is skipped.
- Hook command snippets launch `sys.executable` instead of a bare `python3`
  (absent on Windows), so hooks fire on Windows Claude Code.

---

Releases before 2.7.0 predate this changelog; see the git history for details.
