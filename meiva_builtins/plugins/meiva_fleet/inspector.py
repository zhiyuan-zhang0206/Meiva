# meiva:note
# The fleet's embed in the Inspector Panel: this face declares the agent's per-agent task list (task #2909, reshaped for #3216 on the 2026-09-12 user request) whose rows jump to the task in the fleet task view; the kernel resolves the rows by its ownership rule (base/packages/plugins/surface/inspector.py), so the module carries no ids and no callables. order=250 keeps the work queue directly below the built-in page section (page=200, shells=300) and above the runtime sections, in step with the 2026-10-18 renumbering that the user reported on 2026-10-19 had left the task list above the page.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md

from base.packages.plugins.extensions import PluginContributions
from base.packages.plugins.surface.inspector import InspectWidgetSpec

WIDGETS: tuple[InspectWidgetSpec, ...] = (
    InspectWidgetSpec(
        id="today-tasks",
        kind="taskList",
        order=250,
    ),
)


def contribute() -> PluginContributions:
    """What this plugin declares for the inspector panel."""
    return PluginContributions(inspect_widgets=WIDGETS)
