"""Fleet tasks. Human questions use the core meiva.ui.ask SDK."""

__description__: str = "Fleet task tracking and collaboration guidance."

from base.packages.plugins.extensions import PluginContributions, SdkNamespace

from . import task_registry

# ── meiva.tasks SDK namespace — the task registry (see task_registry.py) ───────
# A whole-module namespace (like meiva_code's meiva.cwd), not members hung on an
# existing group: the registry is a cohesive new surface, and the core top level
# stays small. `expand=True` promotes it into the in-prompt SDK reference
# so agents discover meiva.tasks.* without drilling in.


def contribute() -> PluginContributions:
    """What this plugin declares for the SDK surface."""
    return PluginContributions(
        sdk_namespaces=(SdkNamespace("tasks", task_registry, expand=True),),
    )
