from __future__ import annotations

from pathlib import Path

import pytest

from agent.extensions.catalog import SURFACES, Catalog, PluginView
from base.packages.plugins.contributions import Contribution
from base.packages.plugins.manifest import Dependencies, Lifecycle, PluginManifest
from cli.commands.extensions import plugins_inspect


def _manifest(**contributions: object) -> PluginManifest:
    return PluginManifest(
        name="declared",
        version="1.0.0",
        engines={"meiva": ">=0.1.0"},
        description=None,
        contributions=contributions,
        dependencies=Dependencies(plugins={}, python_packages={}, host_capabilities={}),
        lifecycle=Lifecycle(entry=None, activation="immediate", dispose="effect-registry"),
    )


def _view(
    name: str = "demo",
    *,
    enabled: bool = True,
    contributions: tuple[Contribution, ...] = (),
    manifest: PluginManifest | None = None,
) -> PluginView:
    return PluginView(
        name=name,
        enabled=enabled,
        builtin=True,
        directory=Path("/repo/meiva_builtins/plugins") / name,
        description=f"{name} does things",
        contributions=contributions,
        manifest=manifest if manifest is not None else _manifest(),
    )


def _catalog(*views: PluginView) -> Catalog:
    return Catalog(surfaces=SURFACES, plugins=views)


_HOOK = Contribution(
    surface="hooks", identifier="before_llm", plugin="demo", detail="demo.plugin._DemoHook"
)
_WRAP = Contribution(
    surface="sdkWraps", identifier="files.read", plugin="demo", detail="demo.plugin._wrapped_read"
)
_STATE = Contribution(
    surface="state", identifier="demo__counter", plugin="demo", detail="DemoState.counter: int"
)


def test_no_argument_lists_every_surface_and_every_plugin(capsys: pytest.CaptureFixture[str]):
    catalog = _catalog(_view(contributions=(_HOOK, _WRAP)), _view("quiet", enabled=False))

    assert plugins_inspect.cmd_plugins_inspect(None, catalog=catalog) == 0

    out = capsys.readouterr().out
    for surface in SURFACES:
        assert f"surface {surface.id}\n" in out
    assert "entry        base.packages.plugins.extensions.PluginContributions(" in out
    assert "before_llm: tuple[GraphHook, ...] = ()" in out
    assert "plugin demo  enabled  builtin  hooks 1, sdkWraps 1" in out
    assert "plugin quiet  disabled  builtin  (not loaded)" in out


def test_a_surface_nobody_uses_says_so(capsys: pytest.CaptureFixture[str]):
    catalog = _catalog(_view(contributions=(_HOOK,)))

    plugins_inspect.cmd_plugins_inspect(None, catalog=catalog)

    out = capsys.readouterr().out
    assert "registered   1 — demo 1" in out
    assert "registered   nothing registered here" in out


def test_detail_lists_every_registered_fact(capsys: pytest.CaptureFixture[str]):
    catalog = _catalog(_view(contributions=(_HOOK, _STATE)))

    assert plugins_inspect.cmd_plugins_inspect("demo", catalog=catalog) == 0

    out = capsys.readouterr().out
    assert "registered contributions (2)" in out
    assert "hooks                before_llm                demo.plugin._DemoHook" in out
    assert "state                demo__counter             DemoState.counter: int" in out
    assert "declared 1.0.0  engines.meiva=>=0.1.0" in out
    assert "  registered-not-declared  hooks/before_llm" in out
    assert "plugin_activation" in out


def test_detail_renders_the_diff_and_what_is_not_drift(capsys: pytest.CaptureFixture[str]):
    catalog = _catalog(
        _view(
            contributions=(_HOOK, _WRAP, _STATE),
            manifest=_manifest(hooks=["before_llm", "after_exec"], skills=["a-skill"]),
        ),
    )

    plugins_inspect.cmd_plugins_inspect("demo", catalog=catalog)

    out = capsys.readouterr().out
    assert "  ok                       hooks/before_llm" in out
    assert "  declared-not-registered  hooks/after_exec" in out
    assert "  registered-not-declared  sdkWraps/files.read" in out
    assert "undeclarable state —" in out.replace("\n", " ")
    assert "install-time skills —" in out.replace("\n", " ")


def test_detail_of_a_disabled_plugin_explains_the_empty_list(capsys: pytest.CaptureFixture[str]):
    catalog = _catalog(_view("quiet", enabled=False))

    assert plugins_inspect.cmd_plugins_inspect("quiet", catalog=catalog) == 0

    out = capsys.readouterr().out
    assert "disabled and never imported" in out


def test_an_unknown_plugin_exits_nonzero(capsys: pytest.CaptureFixture[str]):
    catalog = _catalog(_view())

    assert plugins_inspect.cmd_plugins_inspect("nope", catalog=catalog) == 1

    captured = capsys.readouterr()
    assert "no plugin named 'nope'" in captured.err
    assert captured.out == ""
