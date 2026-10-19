# meiva:note
# `build_registry` refuses a plugin whose `meiva-plugin.json` and its `contribute()`
# disagree, but only when the plugin is actually loaded. The plugin's own tests call
# `contribute()` directly, so a new SDK wrap can pass every unit test and then be
# silently skipped in a real agent (the loader is fail-soft). This runs the loader's
# gate for each face this plugin declares, with that face's own manifest keys.
# meiva:link meiva_builtins/plugins/meiva_code/docs/meiva_code.meiva.okf.md
from pathlib import Path


def test_manifest_declares_every_contribution() -> None:
    from base.packages.plugins.gate import check_manifest
    from base.packages.plugins.loading.discovery import FACE_MODULE, SURFACE_MODULE
    from base.packages.plugins.loading.registry import FACE_KEYS
    from meiva_builtins.plugins.meiva_code import agent_runtime, plugin

    plugin_dir = Path(__file__).resolve().parents[1]
    for module, face in ((plugin, SURFACE_MODULE), (agent_runtime, FACE_MODULE)):
        check_manifest("meiva_code", plugin_dir, module.contribute(), FACE_KEYS[face])
