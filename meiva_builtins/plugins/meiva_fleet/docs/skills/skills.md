# Fleet Skills — plugin-carried skills

## What It Is

The `meiva_fleet` plugin, besides registering SDK surfaces (notify / tasks / self), **carries skills**: `plugins/meiva_fleet/skills/` is synced by converge to `~/.meiva/skills/meiva_fleet/` (plugin name = top-level namespace). Agents load them via `meiva.help(meiva.skills.meiva_fleet.<name>)`.

- [meiva_fleet](../../skills/meiva-fleet/README.md) — fleet collaboration master outline: orchestrator decomposition / fork worker / aggregation judgment + task↔fleet interaction rules + communication dichotomy
- [reduce-context-switch-for-human](../../skills/reduce-context-switch-for-human/README.md) — interrupt discipline (added in #620): default queuing via `meiva.ui.ask`, batch by manager subtree, progress rolls up, push only for truly urgent matters

## Notes

- `meiva-fleet` is indexed in every agent's prompt, like every other loaded skill (`skills_to_inject_into_system_prompt` defaults to `*`) — fleet collaboration conventions are one `meiva.help` away from any agent; bodies are always pulled on demand.
- For the skill mechanism and origin axis (origin=plugin), see [Skill System](../../../../../meiva/skills/README.md); for the plugin overview, see [Meiva Fleet](../meiva_fleet.md).
