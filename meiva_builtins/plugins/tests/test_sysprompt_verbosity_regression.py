import io
from collections.abc import Iterator
from contextlib import redirect_stdout

import pytest

import meiva
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install
from meiva.sdk_surface.installation import Installation


def _render_help(target: object) -> str:
    buf = io.StringIO()
    with redirect_stdout(buf):
        meiva.help(target)
    return buf.getvalue()


@pytest.fixture
def load_meiva_code_plugin(model_installation: Installation) -> Iterator[None]:
    from meiva_builtins.plugins.meiva_code import plugin

    install.install(
        ExtensionRegistry((("meiva_code", plugin.contribute()),)),
        catalog=model_installation.require_catalog(),
        authority=model_installation.authority,
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=fixed_sampling_owner(),
    )

    yield

    install.uninstall()


@pytest.fixture
def _load_meiva_fleet_plugin(model_installation: Installation) -> Iterator[None]:
    from meiva_builtins.plugins.meiva_fleet import plugin

    install.install(
        ExtensionRegistry((("meiva_fleet", plugin.contribute()),)),
        catalog=model_installation.require_catalog(),
        authority=model_installation.authority,
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=fixed_sampling_owner(),
    )

    yield

    install.uninstall()


def test_shell_run_wrapper_renders_trimmed_docstring(load_meiva_code_plugin: None) -> None:
    out = _render_help(meiva.shell.run)
    assert "Run a shell command and return its stdout." not in out
    assert "Non-zero exit does not" in out


def test_spawn_wrapper_renders_trimmed_docstring(_load_meiva_fleet_plugin: None) -> None:
    out = _render_help(meiva.agents.spawn)
    assert "Start a new agent and return its id. Does not block." not in out
    assert "does not block" in out
