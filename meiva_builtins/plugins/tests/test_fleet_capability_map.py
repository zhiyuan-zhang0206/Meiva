# meiva:note
# The Fleet plugin's SDK entries all have a capability classification. The core
# coverage test walks whatever surface its process exposes, and a bare test process
# has no `tasks` / `ui.notify`; the plugin checks its own declarations here, where
# they always exist. A new or renamed member fails here before it can reach an
# agent's surface unclassified.

from __future__ import annotations

import inspect

from meiva.impersonation import capabilities as sdk_capabilities
from meiva.sdk_surface.discovery import agent_visible_names
from meiva_builtins.plugins.meiva_fleet import plugin, task_registry


def test_every_declared_entry_has_a_capability() -> None:
    declared = {
        f"tasks.{name}"
        for name in agent_visible_names(task_registry)
        if inspect.isroutine(getattr(task_registry, name, None))
    }
    declared |= {
        f"{member.namespace}.{member.name}"
        for member in plugin.contribute().sdk_members
        if inspect.isroutine(member.fn)
    }
    missing = sorted(declared - set(sdk_capabilities.ENTRY_CAPABILITIES))
    assert missing == [], (
        "the Fleet plugin declares these SDK entries with no capability; "
        f"classify them in meiva/sdk_surface/capabilities.py: {missing}"
    )
