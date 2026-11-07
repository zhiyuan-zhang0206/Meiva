from __future__ import annotations

from agent.graph.hooks.cwd import ValidateCwdAfterInitHook as ValidateCwdAfterInitHook
from agent.state import PluginStateHandle
from base.packages.plugins.extensions import PluginContributions

from ._prompt_sections import coding_tools_section, engineering_workflow_section
from ._state import MeivaCodeState


def state_handle() -> PluginStateHandle[MeivaCodeState]:
    return PluginStateHandle(MeivaCodeState, "meiva_code")


def contribute() -> PluginContributions:
    return PluginContributions(
        system_prompt_sections=(coding_tools_section, engineering_workflow_section),
        state=(MeivaCodeState,),
    )
