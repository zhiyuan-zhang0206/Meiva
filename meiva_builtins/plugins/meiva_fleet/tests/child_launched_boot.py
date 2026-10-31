def _pin_cluster_config() -> None:
    import os

    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV
    from base.packages.plugins.config_registration import plugin_bootstrap_config
    from base.paths import meiva_home

    os.environ[PLUGIN_CLUSTER_CONFIG_ENV] = plugin_bootstrap_config(home=meiva_home())


def launched_imports_share_boot() -> None:
    import importlib
    import inspect
    import os

    os.environ["MEIVA_AGENT_ID"] = "7123"
    os.environ["MEIVA_TIMEZONE"] = "UTC"
    _pin_cluster_config()
    import meiva
    from base.agents.context import MeivaContext
    from base.agents.context.clients import ClientSet
    from meiva.agents import spawn
    from meiva.sdk_surface.installation import Installation, installed

    def _assert_runtime_resources_cold(context: MeivaContext, installation: Installation) -> None:
        bare = installation.clients
        assert isinstance(bare, ClientSet)
        assert bare is not context.clients
        for clients in (context.clients, bare):
            assert clients._sql is None
            assert clients._redis is None
            assert clients._gateway is None
            assert clients._event_pipeline is None
            assert clients._made == {}

    # meiva:note
    # This proves the launched SDK retains its original configuration delivery across later
    # imports. It observes the real environment snapshot, not the number of internal method calls.
    def _assert_captured_timezone(context: MeivaContext, installation: Installation) -> None:
        from meiva.sdk_surface.settings import config_authority

        assert not installation.faces
        assert context.catalog is installation.catalog
        assert installation.clock is not None
        assert config_authority().service_field_value("timezone") == "UTC"
        assert installation.clock.authoritative_timezone == "UTC"
        assert context.require_clock().authoritative_timezone == "UTC"

    context = meiva.context
    installation = installed()
    assert installation is not None
    _assert_captured_timezone(context, installation)
    # meiva:note
    # A new configuration read would fail validation; subsequent imports must use the original
    # valid delivery. The entry does not rewrite or filter this nonempty invalid sentinel.
    os.environ["MEIVA_TIMEZONE"] = "invalid-timezone-after-sdk-boot"
    create = importlib.import_module("meiva.tasks").create
    search = importlib.import_module("meiva.memory").search

    assert "label" in inspect.signature(spawn).parameters
    assert spawn is meiva.agents.spawn
    assert create is meiva.tasks.create
    assert search is meiva.memory.search
    assert meiva.context is context
    assert installed() is installation
    _assert_captured_timezone(context, installation)
    _assert_runtime_resources_cold(context, installation)
    assert installation.sampling.worker is None
    importlib.reload(meiva)
    assert meiva.context is context
    assert installed() is installation
    assert meiva.agents.spawn is spawn
    _assert_captured_timezone(context, installation)
    os.environ["MEIVA_TIMEZONE"] = "UTC"


launched_imports_share_boot()
