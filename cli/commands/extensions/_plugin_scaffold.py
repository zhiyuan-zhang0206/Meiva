# meiva:note
# The plugin scaffold hook — how a plugin owns its own host-side state.
#
# A plugin opts in by shipping `setup.py` beside `plugin.py` with a required
# `scaffold(*, machine: MachineIdentityReader, facts: ScaffoldFacts)`. The explicit `meiva memory
# init` command runs it for every enabled plugin, so runtime memory state is never touched by
# converge or startup. The command reads the settings once and hands them over as `facts`; a
# scaffold reads no settings of its own. A scaffold that does not accept both keywords is refused.
#
# `setup.py` is loaded on its own, NOT through `plugin.py`: plugin modules import
# the agent runtime (hooks, graph, the SDK namespace), none of which exists in a
# CLI process. Keeping the scaffold in a separate module is what lets the explicit
# CLI command call it at all — and it means a scaffold may depend on `base` only.
#
# It lives beside `skills_sync.py` because both load plugin-owned host setup
# without importing the agent runtime.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md

from __future__ import annotations

import importlib.util
import inspect
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from base.cluster.machine import MachineIdentityReader
from base.packages.plugins.loading.scaffold import ScaffoldFacts


@dataclass(frozen=True)
class ScaffoldResult:
    """Which plugins were run by explicit setup. A plugin without `setup.py`
    (or without a `scaffold` in it) is simply absent — not an error."""

    ran: list[str]


# meiva:note
# Call `scaffold(machine=machine, facts=facts)` on every enabled plugin that defines one.
#
# Loading a plugin's `setup.py` is fail-soft (user ruling 2026-09-11): a file that fails to import
# is skipped with a loud report (``base.packages.plugins.load_report``) and the remaining plugins
# still run, because one broken plugin must not block the provisioning command for every other
# plugin. Running a plugin's `scaffold()` stays fail-fast: this is explicit provisioning, and a
# scaffold that raises must stop the command rather than let half-provisioned state pass as
# success.
def run_plugin_scaffolds(*, machine: MachineIdentityReader, facts: ScaffoldFacts) -> ScaffoldResult:
    from base.packages.plugins import enable_config as plugins_cfg
    from base.packages.plugins import load_report

    discovered = plugins_cfg.discover_plugins(home=facts.home)
    config = plugins_cfg.load_for_runtime(set(discovered), home=facts.home)

    ran: list[str] = []
    for name, entry in config.plugins.items():
        if not entry.enabled:
            continue
        setup_py = discovered[name] / "setup.py"
        if not setup_py.is_file():
            continue
        try:
            module = _load_setup_module(name, setup_py)
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            load_report.report_plugin_load_failure(name, exc)
            continue
        scaffold = getattr(module, "scaffold", None)
        if scaffold is None:
            continue
        try:
            inspect.signature(scaffold).bind(machine=machine, facts=facts)
        except TypeError as exc:
            raise TypeError(
                f"plugin {name!r}: scaffold must accept the keywords machine and facts: {exc}"
            ) from exc
        print(f"  · {name}")
        scaffold(machine=machine, facts=facts)
        ran.append(name)
    return ScaffoldResult(ran=ran)


def _load_setup_module(name: str, setup_py: Path) -> ModuleType:
    """Import a plugin's `setup.py` by path. Raises on a load failure — the
    caller reports it through the fail-soft contract."""
    spec = importlib.util.spec_from_file_location(f"meiva_plugin_setup_{name}", setup_py)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"plugin {name!r}: cannot load {setup_py}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
