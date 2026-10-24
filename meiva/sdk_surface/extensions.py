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

from collections.abc import Sequence
from dataclasses import replace

from base.clock import Clock
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.plugins import load_report
from base.packages.plugins.loading import StateGate
from base.packages.plugins.loading.faces import LoadedExtensions, import_faces

from . import install as sdk_install
from .installation import EventPipelines


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
def load_extensions(
    *,
    surface: bool = False,
    report: load_report.Reporter | None = None,
    catalog: ModelCatalog | None = None,
    provider_keys: ProviderKeys | None = None,
    authority: ConfigAuthority | None = None,
    clock: Clock | None = None,
    clients: EventPipelines | None = None,
    disabled_entries: Sequence[str] = (),
    validate_state: StateGate | None = None,
) -> LoadedExtensions:
    previous = sdk_install.uninstall() if not surface else None
    sampling = None
    if previous is not None:
        sampling = previous.sampling
        if clients is None:
            clients = previous.clients
        if clock is None:
            clock = previous.clock

    loaded = import_faces(surface=surface, report=report, validate_state=validate_state)
    admitted = sdk_install.install(
        loaded.registry,
        report,
        config_images=loaded.config_images,
        catalog=catalog,
        provider_keys=provider_keys,
        authority=authority,
        clients=clients,
        sampling=sampling,
        clock=clock,
        disabled_entries=disabled_entries,
    )
    return replace(loaded, registry=admitted)
