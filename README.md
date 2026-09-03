# mycelium-palace

A personal, local-first **memory system for AI agents**. It gives an assistant
(Claude Desktop, Claude Code, Cursor, …) a durable memory across sessions:

- **notes** — curated synthesis you (or the agent) write down
- **drawers** — verbatim captures (command output, pastes, file contents)
- **links** — a typed semantic graph connecting the two

Everything is stored as plain Markdown files on your disk (git-backed, yours to
read), with a PostgreSQL/pgvector index for hybrid keyword + semantic search.
It speaks [MCP](https://modelcontextprotocol.io), so any MCP-capable client can
use it. The PyPI package is `mycelium-palace`; the Python import is `mycelium`.

## Requirements

- **Python 3.12+**
- **PostgreSQL with the `pgvector` extension.** A one-file Docker setup is
  included (`docker-compose.yml`) — you don't need to know Postgres.
- An **MCP client**: Claude Desktop (easiest for a personal setup) or Claude Code.

## Quick start

### 1. Install

```bash
pip install mycelium-palace        # or: uv tool install mycelium-palace
```

### 2. Start the database

From a clone of this repo (or drop the included `docker-compose.yml` anywhere):

```bash
docker compose up -d
```

That runs Postgres with pgvector on `localhost:5432`. mycelium creates its own
tables and the `vector` extension on first connect — nothing else to set up.

> **Windows:** install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)
> (free for personal use; runs on Windows Home via the WSL2 backend). Then run
> the `docker compose up -d` above in PowerShell.

### 3. Wire it into your assistant

**Claude Desktop** (recommended for a personal setup — Windows/macOS/Linux):

```bash
mycelium install-desktop --db-url postgresql://postgres:changeme@localhost:5432/postgres
```

This writes an MCP server entry into Claude Desktop's config
(`%APPDATA%\Claude\claude_desktop_config.json` on Windows;
`~/Library/Application Support/Claude/` on macOS; `~/.config/Claude/` on Linux),
launching mycelium over stdio. **Restart Claude Desktop** to load it. Run with
`--dry-run` first to preview the config.

**Claude Code:**

```bash
mycelium install --auto-hooks      # merges recall/capture hooks into ~/.claude/settings.json
```

### 4. Use it

Ask your assistant to remember something ("capture this…", "make a note that…")
and to recall ("what do we know about…"). Notes and drawers land as Markdown
under `~/.mycelium/data/vault/`.

## How recall reaches the model

This is the one behaviour that differs by client:

- **Claude Code** injects relevant memory **automatically** before every prompt
  via the `UserPromptSubmit` hook. Zero effort.
- **Claude Desktop has no hooks**, so recall is triggered two ways:
  1. **The `context` tool** — the model calls it when it judges memory is
     relevant (its description tells it to, at task start). Reliable but
     model-elective.
  2. **The `/recall` prompt** — pick **recall** from the "+" menu to
     deterministically inject memory into the conversation (optionally with a
     query). This is the manual analogue of the Claude Code hook.

  For more consistent automatic recall, add a line to your Claude Desktop
  **Project custom instructions**: *"At the start of each task, call the
  mycelium `context` tool to recall relevant memory."*

## How agent guidance is distributed

Guidance is authored once and travels with the package — update mycelium and
every connected client picks up the new behaviour:

| Layer | Where it lives | How clients see it |
|---|---|---|
| **Per-tool guidance** (when to call `context()`, capture rules) | `@mcp.tool()` docstrings in `mycelium/server.py` | Sent in `tools/list`; surfaced by all clients incl. Claude Desktop |
| **Server instructions** (broader usage notes) | FastMCP `instructions=` field | Sent in `initialize`; honoured by Claude Code — **ignored by Claude Desktop**, which is why the recall imperative also lives in the `context()` tool description |
| **Hook wiring** (Claude Code) | `~/.claude/settings.json` | `mycelium install --auto-hooks` merges it for you |

## Advanced: running behind a gateway

For multi-client or team setups, mycelium runs behind a gateway like
[contextforge](https://github.com/IBM/mcp-context-forge). `mycelium serve`
exposes SSE/HTTP transports (`--transport sse|streamable-http|stdio`) and
`mycelium install --add-mcp-server` registers a gateway endpoint. The gateway
must forward the upstream `instructions` field; IBM's upstream drops it as of
`c3251f616`, so the [l-v-b fork](https://github.com/l-v-b/mcp-context-forge)
carries a [forwarding patch](https://github.com/l-v-b/mcp-context-forge/pull/1).

## Credits

The verbatim-capture / drawer storage / hybrid BM25+vector search at the heart
of mycelium-palace originated in [mempalace](https://pypi.org/project/mempalace/)
by [milla-jovovich](https://github.com/MemPalace). mycelium-palace folds that
design into a single-process server alongside the curated-note and typed-link
layers; mempalace remains the authoritative source for the verbatim-only use case.

## License

MIT — see [LICENSE](LICENSE).
