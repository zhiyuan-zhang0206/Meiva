from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime

from agent.hooks import Hook
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugins.extensions import PluginContributions
from base.paths import workspace_dir

TO_DO_BETTER_FILENAME = "to_do_better.md"

REMINDER = (
    "If this context window produced a lesson worth keeping — a mistake you made, a wrong "
    "assumption, or a correction from the user — append it to your workspace's "
    f"`{TO_DO_BETTER_FILENAME}`: what went wrong and the specific change to make next time. "
    "Skip it when nothing qualifies; do not restate status, results, or the summary."
)


# meiva:note
# The reminder is conditional, not a per-window obligation: a window with nothing worth keeping writes nothing, and leaving the file alone is not a missed duty. It stays silent when the workspace is not in the system prompt, where the path would name something the agent cannot see.
# meiva:link meiva_builtins/plugins/meiva_self_evolution/docs/meiva_self_evolution.meiva.okf.md
def self_evolution_compact_reminder(slices: AgentSlices, *, catalog: ModelCatalog) -> str:
    del catalog
    if not slices.read("agent", "workspace_in_system_prompt"):
        return ""
    return REMINDER


# meiva:note
# Seeding creates only a missing file and never overwrites one: a lesson recorded in an earlier session survives every restart.
# meiva:link meiva_builtins/plugins/meiva_self_evolution/docs/meiva_self_evolution.meiva.okf.md
def ensure_to_do_better(workspace: Path) -> None:
    path = workspace / TO_DO_BETTER_FILENAME
    if not path.exists():
        path.touch()


class SeedToDoBetterHook(Hook):
    async def __call__(
        self,
        _state: AgentState,
        runtime: Runtime[MeivaContext],
        _config: RunnableConfig,
        /,
    ) -> dict[str, Any] | None:
        identity = runtime.context.identity
        agent_id = None if identity is None else identity.agent_id
        if agent_id is not None:
            ensure_to_do_better(workspace_dir(agent_id))
        return None


def contribute() -> PluginContributions:
    return PluginContributions(
        compact_reminder_sections=(self_evolution_compact_reminder,),
        after_init=(SeedToDoBetterHook(),),
    )
