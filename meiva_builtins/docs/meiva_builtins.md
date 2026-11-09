# Built-ins

`meiva_builtins/` contains packages shipped with the repository. These are
concrete implementations and instruction packages, separate from the generic
extension mechanisms.

- `meiva_builtins/commands/` — bundled Composer prompt templates (`compact`, `recap`),
  discovered by `base/packages/composer_commands.py` in source and wheel installs.
  User templates in `$MEIVA_HOME/commands/` may override them. Planning and scope
  alignment use the existing `meiva-workflow` skill; no built-in `plan` template remains.
- [Built-in plugins](../plugins/README.md) — bundled plugin implementations.
- [Skills catalog](../skills/README.md) — bundled instruction catalog.
- [Built-in MCP integrations](../mcps/docs/mcps.md) — bundled MCP integrations.

Shared package wiring is described by [Extensions](extensions.md).
Mechanism contracts live in [Plugin System](../../docs/plugins/plugins.md),
[Skill System](../../docs/skills/skills.md) and [MCP Integrations](../../docs/mcps/mcps.md).
