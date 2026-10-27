import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import base.packages.mcp_config as cfg_mod
from base.host.system.hostport import Platform
from base.host.system.tests.fake_commands import fake_host
from base.packages.plugins.mcp_enabled import (
    McpEnabledConfig,
    McpEnabledConfigError,
    McpServerEntry,
    write_local,
)
from base.paths import meiva_home
from meiva.tests.fixtures.pin_agent import pin_agent

_MACOS = Platform("darwin", "arm64", "/usr/bin/python3")
_LINUX = Platform("linux", "x86_64", "/usr/bin/python3")


def _write(path: Path, servers: dict[str, dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# meiva:note
# A real plugin directory under the home's external plugin root.
def _write_plugin(home: Path, name: str, servers: dict[str, dict[str, str]]) -> Path:
    mcp_json = home / "plugins" / name / ".mcp.json"
    _write(mcp_json, servers)
    return mcp_json


def test_machine_config_path_uses_meiva_home(unit_home: Path) -> None:
    assert cfg_mod.machine_config_path(home=unit_home) == unit_home / "mcp.json"


def test_read_servers_empty_when_no_file(tmp_path: Path) -> None:
    assert cfg_mod.read_servers(tmp_path / "absent.json") == {}


def test_read_servers_empty_when_no_section(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"other": {}}), encoding="utf-8")
    assert cfg_mod.read_servers(p) == {}


def test_read_servers_parses_section(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    _write(p, {"fs": {"command": "x"}, "github": {"command": "y"}})
    assert cfg_mod.read_servers(p) == {"fs": {"command": "x"}, "github": {"command": "y"}}


def test_read_servers_raises_on_bad_json(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(cfg_mod.MCPError, match="Failed to read"):
        cfg_mod.read_servers(p)


def test_read_servers_raises_when_section_not_dict(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": [1, 2, 3]}), encoding="utf-8")
    with pytest.raises(cfg_mod.MCPError, match="mcpServers field is not a dict"):
        cfg_mod.read_servers(p)


def test_read_servers_raises_when_top_level_not_object(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    with pytest.raises(cfg_mod.MCPError, match="is not a JSON object"):
        cfg_mod.read_servers(p)


def test_read_servers_empty_when_section_null(tmp_path: Path) -> None:
    p = tmp_path / "mcp.json"
    p.write_text(json.dumps({"mcpServers": None}), encoding="utf-8")
    assert cfg_mod.read_servers(p) == {}


def test_plugin_config_paths_finds_installed_plugin(unit_home: Path) -> None:
    mcp_json = unit_home / "plugins" / "myplugin" / ".mcp.json"
    _write(mcp_json, {"fs": {"command": "x"}})
    assert mcp_json in cfg_mod.plugin_config_paths(home=unit_home)


def test_plugin_config_paths_ignores_plugin_without_mcp_json(unit_home: Path) -> None:
    (unit_home / "plugins" / "noplugin").mkdir(parents=True)
    assert all(p.parent.name != "noplugin" for p in cfg_mod.plugin_config_paths(home=unit_home))


def test_load_has_only_the_repo_declared_servers_when_the_home_configures_none(
    unit_home: Path,
) -> None:
    repo_declared: dict[str, dict[str, object]] = {}
    for path in [*cfg_mod.builtin_mcp_paths(), *cfg_mod.plugin_config_paths(home=unit_home)]:
        repo_declared.update(cfg_mod.read_servers(path))

    assert cfg_mod.load_mcp_config(include_disabled=True, home=unit_home) == repo_declared


def test_load_returns_machine_servers(unit_home: Path) -> None:
    _write(unit_home / "mcp.json", {"fs": {"command": "machine"}})
    assert cfg_mod.load_mcp_config(home=unit_home)["fs"] == {"command": "machine"}


def test_load_returns_plugin_servers_when_no_machine_config(unit_home: Path) -> None:
    _write_plugin(unit_home, "plugin", {"fs": {"command": "plugin"}})
    assert cfg_mod.load_mcp_config(home=unit_home)["fs"] == {"command": "plugin"}


def test_machine_overrides_plugin_on_name_collision(unit_home: Path) -> None:
    _write_plugin(unit_home, "plugin", {"fs": {"command": "plugin"}, "extra": {"command": "p"}})
    _write(unit_home / "mcp.json", {"fs": {"command": "machine"}})

    merged = cfg_mod.load_mcp_config(home=unit_home)
    assert merged["fs"] == {"command": "machine"}
    assert merged["extra"] == {"command": "p"}


def test_later_plugin_overrides_earlier(unit_home: Path) -> None:
    _write_plugin(unit_home, "a", {"fs": {"command": "first"}})
    _write_plugin(unit_home, "b", {"fs": {"command": "second"}})
    assert cfg_mod.load_mcp_config(home=unit_home)["fs"] == {"command": "second"}


def test_builtin_mcps_folder_surfaces_chrome(unit_home: Path) -> None:
    merged = cfg_mod.load_mcp_config(home=unit_home)
    assert "chrome" in merged
    assert merged["chrome"]["command"] == ".venv/bin/python"
    assert "services.desktop.browser.mcp_wrapper" in merged["chrome"]["args"]
    assert merged["chrome"]["requires"] == {"display": True}


def test_builtin_command_is_not_uv(unit_home: Path) -> None:
    for name, spec in cfg_mod.load_mcp_config(home=unit_home).items():
        assert spec["command"] != "uv", f"built-in {name} still launches via uv"


def test_machine_config_overrides_builtin(unit_home: Path) -> None:
    _write(unit_home / "mcp.json", {"chrome": {"command": "OVERRIDE"}})
    assert cfg_mod.load_mcp_config(home=unit_home)["chrome"] == {"command": "OVERRIDE"}


def _install_mcp(home: Path, name: str, spec: dict[str, Any]) -> Path:
    from base.packages.extensions import install_registry as reg

    dest = home / "mcps" / name
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".mcp.json").write_text(json.dumps({"mcpServers": {name: spec}}), encoding="utf-8")
    reg.register(reg.InstalledPackage(name=name, type="mcp"), home=home)
    return dest


def test_installed_mcp_surfaces_when_registered(unit_home: Path) -> None:
    _install_mcp(unit_home, "acme", {"command": ".venv/bin/python", "args": ["-m", "acme"]})
    assert "acme" in cfg_mod.load_mcp_config(home=unit_home)


def test_installed_mcp_ignored_without_registry_row(unit_home: Path) -> None:
    _write(unit_home / "mcps" / "stray" / ".mcp.json", {"stray": {"command": "x"}})
    assert "stray" not in cfg_mod.load_mcp_config(home=unit_home)


def test_machine_overrides_installed(unit_home: Path) -> None:
    _install_mcp(unit_home, "acme", {"command": "installed"})
    _write(unit_home / "mcp.json", {"acme": {"command": "machine"}})
    assert cfg_mod.load_mcp_config(home=unit_home)["acme"] == {"command": "machine"}


def test_installed_overrides_plugin(unit_home: Path) -> None:
    _write_plugin(unit_home, "plugin", {"acme": {"command": "plugin"}})
    _install_mcp(unit_home, "acme", {"command": "installed"})
    assert cfg_mod.load_mcp_config(home=unit_home)["acme"] == {"command": "installed"}


def test_installed_mcp_dir_returns_load_dir(unit_home: Path) -> None:
    dest = _install_mcp(unit_home, "acme", {"command": ".venv/bin/python"})
    assert cfg_mod.installed_mcp_dir("acme", home=unit_home) == dest


def test_installed_mcp_dir_none_for_builtin(unit_home: Path) -> None:
    assert cfg_mod.installed_mcp_dir("chrome", home=unit_home) is None


def test_installed_mcp_dir_none_when_machine_shadows(unit_home: Path) -> None:
    _install_mcp(unit_home, "acme", {"command": ".venv/bin/python"})
    _write(unit_home / "mcp.json", {"acme": {"command": "machine"}})
    assert cfg_mod.installed_mcp_dir("acme", home=unit_home) is None


def test_server_cwd_builtin_is_repo_root(unit_home: Path) -> None:
    from base.paths import repo_root

    assert cfg_mod.server_cwd("chrome", home=unit_home) == repo_root()


def test_server_cwd_installed_is_package_dir(unit_home: Path) -> None:
    dest = _install_mcp(unit_home, "acme", {"command": ".venv/bin/python"})
    assert cfg_mod.server_cwd("acme", home=unit_home) == dest


def test_server_cwd_machine_entry_is_none(unit_home: Path) -> None:
    _write(unit_home / "mcp.json", {"solo": {"command": "npx"}})
    assert cfg_mod.server_cwd("solo", home=unit_home) is None


def test_server_cwd_machine_overriding_builtin_is_none(unit_home: Path) -> None:
    _write(unit_home / "mcp.json", {"chrome": {"command": "/abs/python"}})
    assert cfg_mod.server_cwd("chrome", home=unit_home) is None


def test_server_cwd_plugin_is_none(unit_home: Path) -> None:
    _write_plugin(unit_home, "plugin", {"pserver": {"command": "npx"}})
    assert cfg_mod.server_cwd("pserver", home=unit_home) is None


def test_server_cwd_unknown_is_none(unit_home: Path) -> None:
    assert cfg_mod.server_cwd("nosuchserver", home=unit_home) is None


def test_assert_requirements_noop_without_requires() -> None:
    cfg_mod.assert_requirements({"command": "x"}, host=fake_host())


def test_assert_requirements_display_ok_with_display() -> None:
    cfg_mod.assert_requirements({"requires": {"display": True}}, host=fake_host(platform=_MACOS))


# meiva:note
# A Linux host has a display when its environment names one (`DISPLAY` for X11, `WAYLAND_DISPLAY`
# for Wayland) and has none otherwise: the host decides, not the environment of the test process.
@pytest.mark.parametrize("name", ["DISPLAY", "WAYLAND_DISPLAY"])
def test_assert_requirements_display_ok_on_a_linux_host_whose_environment_names_one(
    name: str,
) -> None:
    host = fake_host(platform=_LINUX, environ={name: ":7"})
    cfg_mod.assert_requirements({"requires": {"display": True}}, host=host)
    assert cfg_mod.server_capability({"requires": {"display": True}}, host=host) == (True, None)


def test_assert_requirements_display_raises_on_a_linux_host_without_one() -> None:
    host = fake_host(platform=_LINUX, environ={"DISPLAY": ""})
    with pytest.raises(cfg_mod.MCPError, match="requires a display"):
        cfg_mod.assert_requirements({"requires": {"display": True}}, host=host)
    capable, reason = cfg_mod.server_capability({"requires": {"display": True}}, host=host)
    assert capable is False
    assert reason is not None and "display" in reason


def test_assert_requirements_unknown_key_fails_fast() -> None:
    with pytest.raises(cfg_mod.MCPError, match="unknown requires key"):
        cfg_mod.assert_requirements({"requires": {"gpu": True}}, host=fake_host())


# meiva:note
# On a headless Linux host a probe-first order would report a missing display instead.
@pytest.mark.parametrize(
    "requires",
    [
        [],
        ["display"],
        "",
        "display",
        0,
        1,
        False,
        {"gpu": False},
        {"gpu": True},
        {"display": "false"},
        {"display": 1},
        {"unix_socket": True},
    ],
)
@pytest.mark.parametrize("reader", [cfg_mod.assert_requirements, cfg_mod.server_capability])
def test_invalid_requirements_fail_as_declarations_not_as_a_missing_display(
    requires: object, reader: Callable[..., object]
) -> None:
    with pytest.raises(cfg_mod.MCPError) as caught:
        reader({"requires": requires}, host=fake_host(platform=_LINUX))
    assert "requires a display" not in str(caught.value)


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"requires": None},
        {"requires": {}},
        {"requires": {"display": False}},
    ],
)
def test_optional_or_false_requirements_need_no_display(spec: dict[str, Any]) -> None:
    host = fake_host(platform=_LINUX)
    cfg_mod.assert_requirements(spec, host=host)
    assert cfg_mod.server_capability(spec, host=host) == (True, None)


def test_server_capability_ok_when_host_has_a_display() -> None:
    assert cfg_mod.server_capability(
        {"requires": {"display": True}}, host=fake_host(platform=_MACOS)
    ) == (True, None)


def test_server_url_none_for_stdio_server() -> None:
    assert cfg_mod.server_url({"command": "npx", "args": ["-y", "x"]}) is None
    assert cfg_mod.server_url({}) is None


def test_server_url_accepts_https_endpoint() -> None:
    assert (
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp"}) == "https://mcp.example.com/mcp"
    )
    assert cfg_mod.server_url({"url": "http://localhost:8080/mcp"}) == "http://localhost:8080/mcp"


def test_server_url_rejects_non_http_url() -> None:
    with pytest.raises(cfg_mod.MCPError, match=r"http\(s\) endpoint"):
        cfg_mod.server_url({"url": "file:///tmp/x"})
    with pytest.raises(cfg_mod.MCPError, match=r"http\(s\) endpoint"):
        cfg_mod.server_url({"url": 42})


def test_server_url_rejects_url_plus_command() -> None:
    with pytest.raises(cfg_mod.MCPError, match="both 'url' and 'command'"):
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "command": "npx"})


def test_server_url_rejects_non_dict_headers() -> None:
    with pytest.raises(cfg_mod.MCPError, match="'headers' must be a dict"):
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "headers": ["Bearer x"]})
    with pytest.raises(cfg_mod.MCPError, match="'headers' must be a dict"):
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "headers": {"k": 1}})


def test_server_url_accepts_string_headers() -> None:
    assert (
        cfg_mod.server_url(
            {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer x"}}
        )
        == "https://mcp.example.com/mcp"
    )


def test_server_url_accepts_oauth_flag() -> None:
    assert (
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "oauth": True})
        == "https://mcp.example.com/mcp"
    )


def test_server_url_rejects_non_bool_oauth() -> None:
    with pytest.raises(cfg_mod.MCPError, match="'oauth' must be a boolean"):
        cfg_mod.server_url({"url": "https://mcp.example.com/mcp", "oauth": "yes"})


def test_server_url_rejects_oauth_plus_headers() -> None:
    with pytest.raises(cfg_mod.MCPError, match="pick one auth mode"):
        cfg_mod.server_url(
            {"url": "https://mcp.example.com/mcp", "oauth": True, "headers": {"k": "v"}}
        )


def test_builtin_mcps_folder_surfaces_computer_use(unit_home: Path) -> None:
    merged = cfg_mod.load_mcp_config(home=unit_home)
    assert "computer_use" in merged
    assert merged["computer_use"]["shared"] == "computer_use"
    assert merged["computer_use"]["command"] == ".venv/bin/python"
    assert "services.desktop.computer.mcp_wrapper" in merged["computer_use"]["args"]
    assert merged["computer_use"]["requires"] == {"display": True}


# meiva:note
# ── per-host enable overlay (`mcp_enabled.json`) ──────────────────────────────
#
# The loader drops servers the per-host overlay marks disabled by default;
# `include_disabled=True` returns the full merged map. A server absent from the overlay stays
# (default-on). The repo's built-in and plugin servers load as they are; each test looks only at
# the machine servers it wrote.


def _write_machine(unit_home: Path, servers: dict[str, dict[str, str]]) -> None:
    (unit_home / "mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# meiva:note
# The machine config is the one under the home the loader is handed, not the process's own.
def test_the_machine_config_is_the_one_under_the_home_it_is_handed(
    unit_home: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other-home"
    other.mkdir()
    _write_machine(other, {"only-here": {"command": "x"}})

    assert "only-here" in cfg_mod.load_mcp_config(home=other)
    assert "only-here" not in cfg_mod.load_mcp_config(home=unit_home)


# meiva:note
# The names of the servers the loader returns that only the machine config declares.
def _machine_names(unit_home: Path, *, include_disabled: bool = False) -> set[str]:
    repo_declared: set[str] = set()
    for path in [*cfg_mod.builtin_mcp_paths(), *cfg_mod.plugin_config_paths(home=unit_home)]:
        repo_declared |= set(cfg_mod.read_servers(path))
    return (
        set(cfg_mod.load_mcp_config(include_disabled=include_disabled, home=unit_home))
        - repo_declared
    )


@pytest.fixture
def _two_servers(unit_home: Path) -> Path:
    """A machine mcp.json defining two servers."""
    _write_machine(unit_home, {"fs": {"command": "a"}, "github": {"command": "b"}})
    return unit_home


# meiva:note
# No overlay file -> every defined server stays (default-on).
def test_no_overlay_keeps_all(_two_servers: Path) -> None:
    assert _machine_names(_two_servers) == {"fs", "github"}


# meiva:note
# Default load() drops a server the overlay marks disabled; the other stays.
def test_default_drops_disabled(_two_servers: Path) -> None:
    write_local(
        McpEnabledConfig(mcp_servers={"fs": McpServerEntry(enabled=False)}), home=meiva_home()
    )
    assert _machine_names(_two_servers) == {"github"}


# meiva:note
# include_disabled=True returns the full merged map regardless of the overlay.
def test_include_disabled_keeps_both(_two_servers: Path) -> None:
    write_local(
        McpEnabledConfig(mcp_servers={"fs": McpServerEntry(enabled=False)}), home=meiva_home()
    )
    assert _machine_names(_two_servers, include_disabled=True) == {"fs", "github"}


# meiva:note
# An explicit enabled=True entry leaves the server in place.
def test_overlay_enabled_true_keeps_server(_two_servers: Path) -> None:
    write_local(
        McpEnabledConfig(mcp_servers={"fs": McpServerEntry(enabled=True)}), home=meiva_home()
    )
    assert _machine_names(_two_servers) == {"fs", "github"}


@pytest.mark.parametrize("content", ["{not json", '{"mcp_servers":{"fs":{"enabled":[]}}}'])
def test_invalid_overlay_raises_instead_of_hiding_servers(_two_servers: Path, content: str) -> None:
    (_two_servers / "mcp_enabled.json").write_text(content)
    with pytest.raises(McpEnabledConfigError):
        cfg_mod.load_mcp_config(home=meiva_home())


def test_definition_inventory_can_inspect_disabled_entries_with_invalid_overlay(
    _two_servers: Path,
) -> None:
    (_two_servers / "mcp_enabled.json").write_text("{not json")
    assert _machine_names(_two_servers, include_disabled=True) == {"fs", "github"}


def test_sdk_servers_surfaces_the_overlay_error(_two_servers: Path) -> None:
    from meiva import mcps

    pin_agent(1, meiva_home=_two_servers)
    (_two_servers / "mcp_enabled.json").write_text("{not json")
    with pytest.raises(McpEnabledConfigError):
        mcps.servers()
    with pytest.raises(McpEnabledConfigError):
        _ = mcps.fs
