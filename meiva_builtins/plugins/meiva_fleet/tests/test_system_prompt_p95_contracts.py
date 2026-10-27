from __future__ import annotations

from collections.abc import Iterator

import pytest

from agent.graph.prompt.live import SectionInputs, read_live_sdk
from agent.graph.prompt.system_prompt import sdk_expand_section
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.config import FIELD_INFOS
from base.config.service_read import ConfigAuthority
from base.config.tests.config_authority import config_authority as config_authority
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths import meiva_home
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install
from meiva.sdk_surface.installation import installed


@pytest.fixture(autouse=True)
def _no_plugin_expansions() -> Iterator[None]:
    prior = installed()
    install.uninstall()
    yield
    install.uninstall()
    if prior is not None:
        install.install(
            prior.registry,
            catalog=prior.catalog,
            provider_keys=prior.provider_keys,
            authority=prior.authority,
            config_images=prior.config_images,
            sampling=fixed_sampling_owner(),
        )


def test_p95_contracts_leave_rare_namespaces_on_demand(
    capsys: pytest.CaptureFixture[str],
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
) -> None:
    from meiva_builtins.plugins.meiva_fleet import plugin as fleet

    factory = FIELD_INFOS["sdk_expand_in_system_prompt"].default_factory
    assert factory is not None
    install.install(
        ExtensionRegistry((("meiva_fleet", fleet.contribute()),)),
        catalog=model_catalog,
        authority=config_authority,
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=fixed_sampling_owner(),
    )

    slices = AgentSlices.resolve(
        default_reader=defaults(agent__sdk_expand_in_system_prompt=factory())
    )

    text = sdk_expand_section(
        SectionInputs(slices, model_catalog, read_live_sdk(slices.prompt, home=meiva_home()))
    )

    for module in ["shell", "files", "agents", "tasks"]:
        assert f"## meiva.{module}\n" in text
    for module in ["memory", "self", "ui", "watcher", "web", "shell.sessions"]:
        assert f"## meiva.{module}\n" not in text
    import meiva

    meiva.help(meiva.watcher)
    assert "def launch(" in capsys.readouterr().out
    meiva.help(meiva.shell.sessions)
    assert "## meiva.shell.sessions" in capsys.readouterr().out
