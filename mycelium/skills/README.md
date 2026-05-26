# Mycelium skills (in-package)

Skills shipped inside the `mycelium-palace` PyPI package. Anything in this
directory is bundled into the wheel and copied to `~/.mycelium/skills/mycelium/`
on `mycelium install`. The `mycelium install` step also adds that path to the
target client's `skillsDirectories` (Claude Code) so the skills become
discoverable without further configuration.

## Layout

```
mycelium/skills/
  README.md                  # this file
  <skill-name>/
    SKILL.md                 # required: skill body with frontmatter
    <supporting files>       # optional: scripts, templates, etc.
```

Each subdirectory is a single skill. Claude Code expects a `SKILL.md` with
YAML frontmatter (at minimum a `description` field) and the skill body.

## What belongs here

Skills *about using mycelium itself* — e.g. curating notes, link hygiene,
capture conventions worth a slash command. Things you'd want every client
that installs mycelium-palace to know how to do.

## What does NOT belong here

- **Personal portable skills** (workflow, fitness logging, etc.) — those live
  in a separate `l-v-b/personal-skills` repo, synced via `mycelium skills sync`
  (roadmap 3.1.6 B).
- **Machine-local skills** (host-specific display layouts, hardware quirks) —
  stay under `~/.claude/skills/` on the relevant machine.
- **Built-in Claude Code skills** (init, review, loop, schedule, etc.) — don't
  re-vendor; the harness ships them.

## Resource exposure

The mycelium MCP server registers each skill here as an MCP resource at
`mycelium-skill://<slug>` so any MCP client connecting to mycelium (directly
or via ContextForge) can enumerate the canonical set via `resources/list`.
The filesystem copy at `~/.mycelium/skills/mycelium/` is the cache; this
directory is the source of truth.
