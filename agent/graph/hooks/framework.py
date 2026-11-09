# meiva:note
# Compaction, crash repair, the capability-index drift check, and skill recall are core capabilities, not plugins: unconditional, and independent of the plugin enable config. Before the turn, repair runs before plugins so its full-history replacement cannot erase their appended notes while committing their other state updates. The remaining framework hooks follow plugins in their existing order. Every hook still reads the same original state; only the order in which their returned deltas merge changes.
# meiva:link agent/hooks/docs/hooks.md

from agent.graph.hooks.capabilities import newly_installed_skills
from agent.graph.hooks.compact import compact_reminder
from agent.graph.hooks.skill_recall import SkillRecallHook
from agent.hooks import Hook
from agent.hooks.repair import repair_dangling_tool_pairing
from base.packages.plugins.extensions import ExtensionRegistry, GraphHook, HookPoint


def framework_hooks() -> dict[HookPoint, tuple[Hook, ...]]:
    return {
        "before_turn": (
            repair_dangling_tool_pairing,
            compact_reminder,
            SkillRecallHook(),
            newly_installed_skills,
        ),
    }


def hooks_at(point: HookPoint, extensions: ExtensionRegistry) -> list[tuple[str | None, GraphHook]]:
    framework = framework_hooks()[point]
    return [
        (None, repair_dangling_tool_pairing),
        *extensions.hooks(point),
        *((None, hook) for hook in framework if hook is not repair_dangling_tool_pairing),
    ]
