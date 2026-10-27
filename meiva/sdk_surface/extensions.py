"""Load the enabled plugins and install what they declare into the SDK surface.

Base owns discovery and loading (`base.packages.plugins.loading`: which plugins are enabled,
importing their faces, building the registry of what they declare). This module only installs
that result into `meiva`: it uninstalls the previous load's SDK surface on a full load, asks base
for the faces, and hands the registry to `meiva.sdk_surface.install`, the one writer of the `meiva`
module. A plugin whose agent-runtime face declares state is validated by the caller's
`validate_state` (`agent.state.plugin_state_schema`), which only a process that runs agent code
can supply.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Unpack

from base.packages.plugins.loading import StateGate
from base.packages.plugins.loading.faces import LoadedExtensions, import_faces

from . import install as sdk_install
from .installation import InstallOptions, SamplingPolicyOwner, installed
from .pure.plans.installation.lifecycle_plan import carry_over


# meiva:note
# Keyword arguments accepted by `load_extensions` on top of `InstallOptions`.
class LoadExtensionsOptions(InstallOptions, total=False):
    report: sdk_install.Reporter | None
    sampling: SamplingPolicyOwner | None
    validate_state: StateGate | None


# meiva:note
# Read plugins_config.json, import the enabled plugins' faces, install what they declare.
#
# Full form (`surface=False`, the default): uninstall the previous round's SDK surface, import every
# enabled plugin's `plugin.py` followed by its `agent_runtime.py` face, build the gated registry of
# what the faces declare and install its SDK surface into `meiva` (the one place `meiva` is written:
# `meiva.sdk_surface.install`). Called at host boot and by the agent-side tooling. The returned registry
# holds the plugins admitted — one the install refused (a namespace conflict, a bad wrap target, a
# config that does not bind) is reported and absent.
#
# Surface form (`surface=True`): import only the `plugin.py` surfaces and install the surface-only
# registry — the load an agent-launched child runs. No uninstall: a child loads once per process and
# never runs the agent runtime, and the graph/LM stacks stay off its boot path.
#
# Contained plugin failures go to ``report`` (see `import_faces`). Model and configuration consumers
# receive their composition root's explicit ``catalog``, ``provider_keys`` and ``authority``.
# Inventory-only loads supply none of them; accessing those SDK capabilities still requires a bound owner.
# The root names the ``sampling`` owner the installation retains and closes. A full load that
# replaces an installation carries that installation's owner, clients and clock over unless the
# caller names its own; a first load has nothing to carry, so it refuses to run without an owner.
# ``external_caller`` is the opt-in external tool profile its root read from the process once;
# none means the process declared no external caller.
def load_extensions(
    *,
    surface: bool = False,
    home: Path,
    **options: Unpack[LoadExtensionsOptions],
) -> LoadedExtensions:
    report = options.get("report")
    carried = carry_over(
        installed() if not surface else None,
        sampling=options.get("sampling"),
        clients=options.get("clients"),
        clock=options.get("clock"),
    )

    if not surface:
        sdk_install.uninstall(retaining_sampling=carried.sampling)

    loaded = import_faces(
        surface=surface, report=report, validate_state=options.get("validate_state"), home=home
    )
    admitted = sdk_install.install(
        loaded.registry,
        report,
        config_images=loaded.config_images,
        catalog=options.get("catalog"),
        provider_keys=options.get("provider_keys"),
        authority=options.get("authority"),
        clients=carried.clients,
        sampling=carried.sampling,
        clock=carried.clock,
        external_caller=options.get("external_caller"),
        disabled_entries=options.get("disabled_entries", ()),
    )
    return replace(loaded, registry=admitted)
