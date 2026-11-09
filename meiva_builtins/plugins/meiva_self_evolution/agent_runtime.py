from __future__ import annotations

from agent.graph.hooks.cwd import (
    TO_DO_BETTER_FILENAME as TO_DO_BETTER_FILENAME,
)
from agent.graph.hooks.cwd import (
    ensure_to_do_better as ensure_to_do_better,
)
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import PluginContributions

REMINDER = (
    "If this context window produced a lesson worth keeping — a mistake you made, a wrong "
    "assumption, or a correction from the user — append it to your workspace's "
    f"`{TO_DO_BETTER_FILENAME}`: what went wrong and the specific change to make next time. "
    "Skip it when nothing qualifies; do not restate status, results, or the summary."
)


# meiva:note
# The reminder is conditional, not a per-window obligation: a window with nothing worth keeping writes nothing, and leaving the file alone is not a missed duty. It stays silent when the workspace is not in the system prompt, where the path would name something the agent cannot see.
# meiva:link meiva_builtins/plugins/meiva_self_evolution/README.md
def self_evolution_compact_reminder(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    del catalog
    if not slices.read("agent", "workspace_in_system_prompt"):
        return ""
    return REMINDER


def contribute() -> PluginContributions:
    return PluginContributions(
        compact_reminder_sections=(self_evolution_compact_reminder,),
    )
