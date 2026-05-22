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
mycelium --help
```

Full setup docs forthcoming.

## License

MIT — see [LICENSE](LICENSE).
