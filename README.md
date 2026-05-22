# mycelium-palace

A unified memory system for AI agents — verbatim captures (drawers), curated synthesis notes, and a typed semantic link graph between them. Speaks MCP, designed for use behind a gateway like [contextforge](https://github.com/IBM/mcp-context-forge).

Successor to [mempalace](https://github.com/MemPalace/mempalace) — same single-process, disk-first, git-backed Markdown-vault design, but with notes/drawers/links unified under one server instead of two.

## Why "mycelium-palace"?

The package combines the curated-knowledge layer originally called *mycelium* and the verbatim-storage layer originally called *mempalace* into one coherent system. The PyPI name reflects that lineage; the Python import path is just `mycelium`.

## Status

Production-deployed on the author's personal stack since 2026-05. Phase 3 (Rain team k8s deployment) is in design.

## Quick start

```bash
uv tool install mycelium-palace
mycelium install --auto-hooks    # writes ~/.mycelium/hooks/, merges into ~/.claude/settings.json
mycelium --help
```

## How agent guidance is distributed

Three layers, each authored once and reaching every connected client without manual sync:

| Layer | Where it lives | How clients see it |
|---|---|---|
| **Always-on usage guidance** (when to call `context()`, capture conventions, link patterns) | FastMCP `instructions=` field in `mycelium/server.py` | Sent in every MCP `initialize` response; surfaced by clients (Claude Code shows as `# MCP Server Instructions` system block) |
| **Per-tool descriptions** | `@mcp.tool()` docstrings | Sent in `tools/list` response |
| **Host-specific bits** (which gateway prefix, hook caveats, NL → tool-name disambiguation) | `~/.claude/CLAUDE.md` (or analogue for other clients) | Manual paste, ~8 lines |
| **Hook wiring** | `~/.claude/settings.json` | `mycelium install --auto-hooks` merges it for you |

The first three are *server-authored* and shipped with the package — update the package, every client picks up the new guidance automatically.

When running behind a gateway like [contextforge](https://github.com/IBM/mcp-context-forge), the gateway must forward the upstream `instructions` field. The official IBM/mcp-context-forge upstream silently drops it as of `c3251f616`; the [l-v-b fork](https://github.com/l-v-b/mcp-context-forge) carries the [forwarding patch](https://github.com/l-v-b/mcp-context-forge/pull/1).

## License

MIT — see [LICENSE](LICENSE).
