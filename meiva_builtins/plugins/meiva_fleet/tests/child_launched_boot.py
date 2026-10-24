import gc
from typing import Any, cast

from base.config import ConfigBoot


def _assert_runtime_resources_cold(context: Any, installation: Any) -> None:
    bare = installation.clients
    assert bare is not None
    assert bare is not context.clients
    for clients in (context.clients, bare):
        assert clients._sql is None
        assert clients._redis is None
        assert clients._gateway is None
        assert clients._event_pipeline is None
        assert clients._made == {}


def _pin_fleet_cluster_config() -> None:
    import json
    import os

    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV
    from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig

    fleet = FleetConfig()
    projection = {
        name: getattr(fleet, name)
        for name, info in FleetConfig.model_fields.items()
        if cast("dict[str, object]", info.json_schema_extra).get("scope")
        in {"cluster-pinned", "cluster-default"}
    }
    os.environ[PLUGIN_CLUSTER_CONFIG_ENV] = json.dumps({"meiva_fleet": projection})


def _live_boots() -> list[ConfigBoot]:
    return [candidate for candidate in gc.get_objects() if type(candidate) is ConfigBoot]


def _assert_shared_boot(context: Any, installation: Any) -> None:
    assert not installation.faces
    assert context.catalog is installation.catalog
    assert installation.clock is not None
    boots = _live_boots()
    assert len(boots) == 1, boots
    boots[0].set_field("timezone", "Asia/Tokyo")
    assert context.require_clock().authoritative_timezone == "Asia/Tokyo"


def launched_imports_share_boot() -> None:
    import importlib
    import inspect
    import os

    _pin_fleet_cluster_config()
    os.environ["MEIVA_AGENT_ID"] = "7123"
    import meiva
    from meiva.agents import spawn
    from meiva.sdk_surface.installation import installed

    create = importlib.import_module("meiva.tasks").create
    search = importlib.import_module("meiva.memory").search

    assert "label" in inspect.signature(spawn).parameters
    assert spawn is meiva.agents.spawn
    assert create is meiva.tasks.create
    assert search is meiva.memory.search
    context = meiva.context
    installation = installed()
    assert installation is not None
    _assert_shared_boot(context, installation)
    _assert_runtime_resources_cold(context, installation)
    assert installation.sampling.worker is None
    importlib.reload(meiva)
    assert meiva.context is context
    assert installed() is installation
    assert meiva.agents.spawn is spawn
    assert len(_live_boots()) == 1


launched_imports_share_boot()
