"""Fleet labels and tasks. Human questions use the core meiva.ui.ask SDK."""

__description__: str = "Fleet role labels, task tracking and collaboration guidance."

import meiva
import meiva.sdk_surface.agent_identity
from base.packages.plugins.extensions import PluginContributions, SdkMember, SdkNamespace
from meiva.sdk_surface.validation import coerce_str

from . import task_registry


def set_label(text: str) -> None:
    text = coerce_str(text, "text", allow_none=True)
    agent_id = meiva.sdk_surface.agent_identity.require_agent_id()
    from meiva import gateway_client

    gateway_client.patch_label(agent_id, text or "", source="self")


# ── meiva.tasks SDK namespace — the task registry (see task_registry.py) ───────
# A whole-module namespace (like meiva_code's meiva.cwd), not members hung on an
# existing group: the registry is a cohesive new surface, and the core top level
# stays small. `expand=True` promotes it into the in-prompt SDK reference
# so agents discover meiva.tasks.* without drilling in.


def contribute() -> PluginContributions:
    """What this plugin declares for the SDK surface."""
    return PluginContributions(
        sdk_namespaces=(SdkNamespace("tasks", task_registry, expand=True),),
        sdk_members=(SdkMember("self", "set_label", set_label),),
    )
