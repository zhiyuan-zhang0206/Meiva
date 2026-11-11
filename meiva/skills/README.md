# Skill System

Skills are reusable instruction packs that agents load at runtime. Each repo
skill is a self-contained directory under `meiva_builtins/skills/<group>/<name>/`
(`SKILL.md` + optional `scripts/` / `references/` / `assets/`); the full
catalog, grouped by what each skill does, is
[the skills index](../../meiva_builtins/skills/README.md).

## How Skills Work
`meiva.help(meiva.skills.<name>)` shows a skill's name, description and SKILL.md path; the agent reads that file.
Each skill is a Markdown file with frontmatter describing when to use it.
Skills can be nested; every agent-visible spelling (index lines, `meiva.help` headings, preloaded-skill sections) is the loadable path, e.g. `meiva.skills.meiva_memory.consolidation`.


## See Also
- [Plugin System](../../docs/plugins/plugins.md) — skills vs plugins distinction

# meiva.skills — Skill Registry

## What it is

`meiva.skills` is a registry of "skill packages" that agents can load, organized as a **namespace tree mirroring the folder structure**. Each SKILL.md is a leaf node, with its parent folders forming its namespace path. Skills are not code; they are instruction text that agents actively read and follow.

## Namespace Tree

Node three states (like Python packages):
- **Leaf skill**: Only SKILL.md, no children with skills → pure leaf. `meiva.skills.meiva_goal`.
- **root skill**: Has both SKILL.md and children with skills → both callable and a namespace. `meiva.help` shows its own entry (description, SKILL.md path) then lists its children the same way.
- **Pure namespace**: Only INDEX.md (or neither) → just a labeled layer with a description. `meiva.skills.coding.tdd`, `meiva.skills.superpowers.brainstorming`.

## Naming: dash outside, underscore inside

**Dash is canonical** at every human-facing surface — the on-disk directory
name, SKILL.md frontmatter `name:`, CLI arguments, gateway API fields, the
`skills_to_inject_into_system_prompt` / `skills_to_expand_at_start` config
lists, the frontend, `identifier()`. That is the Agent Skills / Claude Code
ecosystem spelling: a skill directory authored here is a skill directory
anywhere else, and one published elsewhere drops in unmodified.

**Underscore is the Python projection and nothing else.** `-` is not an
identifier character, so the CodeAct namespace reaches
`inspect-a-trace/` as `meiva.skills.inspect_a_trace`. `target()`
renders that projection; `.` separates namespace segments there, while the
display `identifier()` (composer commands, CLI) joins the same segments with `:`
(`web-ai:deep-research`). Agent-visible text uses the `target()` spelling only.

`base/packages/skills/names.py` is the single fold between them — `match_key` inbound
(dash→underscore, and `:`→`.` so an ecosystem-style `meiva-memory:consolidation` resolves),
`display_name` outbound, `find` where a name has to become a real directory or
registry key. Every dash/underscore comparison in the system routes through it,
which is why a legacy underscore directory (a hand-installed
`~/.meiva/skills/page_ocr/`) still loads, still answers to
`meiva.skills.page_ocr`, and still displays as `page-ocr`. Two directories
that fold together are refused outright (`SkillNameCollision`) rather than one
silently winning.

**Plugin directories are the deliberate exception: they stay underscore on
disk** (`meiva_builtins/plugins/meiva_code/`), because they are real Python
packages — `__init__.py`, `from . import _code_namespace`, and `python -m
meiva_builtins.plugins.meiva_fleet.task_maintenance.daemon` all require a legal
module path. Name ≠ directory here: `identifier()` folds the namespace segment
so the surface still reads `meiva-memory:consolidation` and `meiva-fleet`, and the skills a
plugin carries are dash-named like any other.

## Core API

- `meiva.skills.<path>` — Access a skill or namespace (module `__getattr__` lazy resolution). Resolution is exposure, not use — it records nothing and reads no file.
- `meiva.help(meiva.skills.<path>)` — Name, description and path, nothing else; the agent reads the file for the body. A skill and a root skill (a folder with a SKILL.md and skill children) show their description and the `SKILL.md` path; a namespace labelled by an INDEX.md shows the INDEX.md text and the folder path; a bare folder (no description source) shows the folder path alone. A root skill or namespace then lists its direct children the same way (a child namespace is not expanded); each entry is a heading with the loadable `meiva.skills.<path>`, the description and `Path: <absolute path>`. The path is absolute, so `meiva.files.read` and a native harness's file tool take it as given. A node's folder is recorded on the tree (`NS.path`, written by `SkillIndexBuilder`).
- `meiva.help(meiva.skills)` / `dir(meiva.skills)` — the top level only: each top-level skill or namespace as one entry, none expanded. The agent-visible surface is the `__all_for_meiva__` property on `_SkillsModule` (the module's own class) — `agent_visible_names` reads it with `getattr_static`, which a PEP 562 module `__getattr__` cannot serve.
- Attribution: the `skill_invoked` event at depth `loaded` (the only depth meiva_self_evolution scores as real use) is written when the agent reads a SKILL.md through `meiva.files.read` (`record_skill_invoked_by_path`, an `audit_events` row, then the unified stream); `meiva.help` and node resolution record nothing. A read that bypasses `meiva.files.read` (a shell `cat`, an external harness's own file tool) is not recorded. The same point notes the skill's identifier in the exec turn's state update (`meiva.state_update["skill_reads"]`, plain strings, once per skill per exec) so skill recall knows the body is already in the window; outside an exec turn nothing is noted. The SDK keeps no in-process buffer or dedup set, and a failed write logs an error with traceback and emits `audit_write_failed` without failing the read.
- Plugin skill sources: a plugin declares `skill_sources` (`provider() -> list[Path]`) in `contribute()`; the SDK install adds them and the scan reads them (project-local, scanned last, can override same-named built-ins).

## Loading Sources

`~/.meiva/skills/` is the sole load directory, synced by the converge step (`meiva start` / `meiva update` / `meiva converge`) from repo skills (`<repo>/meiva_builtins/skills/*`) and plugin skills (`<repo>/meiva_builtins/plugins/<p>/skills/*`) (plugin skills land in `~/.meiva/skills/<p>/…`, namespace layer is the folder layer). Top-level directories must be enabled in the install-registry (`meiva skill register <name>`) or are ignored. Duplicate SKILL.md files across paths are deduplicated by full-file SHA256 (including frontmatter).

A mount root's `INDEX.md` is ignored; its `SKILL.md` is retained. Auto-promotion
of a same-named child preserves the old child access path but omits its duplicate
flat-list entry. An existing root skill is not replaced.

Direct skill-file reads, including partial reads, attribute each read.
`meiva/tests/skills/test_skills_attribution.py` checks these boundaries; its failed
audit-write case checks the returned body and logged traceback, not live telemetry
delivery. Mount fixtures in `test_skills_mount.py` compare raw file content.

## Key Dependencies
- [System Prompt — System Prompt](../../agent/graph/docs/system-prompt/system-prompt.md) — the skill index is injected into the system prompt by the **agent process itself** (`agent/graph/prompt/capabilities.py:_skill_index_lines` scans local `~/.meiva/skills`; `system_prompt.py` imports the rendered sections from `capabilities`), once, into `# Capabilities` (wildcard catalogs fold descendants under real entry skills; explicit lists stay verbatim) — `MEIVA_SDK_EXPAND`'s `*` skips `meiva.skills` so the expanded SDK reference does not render a second index; gateway only reuses `base.packages.composer_commands.CommandCatalog` (over `meiva.skills.names`) for `/`-autocomplete, does not inject prompt

## Notes
Skills are essentially instruction text that agents actively load and read, not passively injected plugins (plugins are that). Which skills are available is determined at runtime/config (converge + install-registry), not enumerated in `meiva/` source. Malformed SKILL.md are skipped during scanning with a loud warning, without dragging down the entire fleet.
