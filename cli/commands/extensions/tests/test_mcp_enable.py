# meiva:note
# `meiva mcp enable/disable` write the per-machine MCP-enable overlay, and `meiva mcp list` still
# shows defined-but-disabled servers with a marker. `unit_home` isolates ~/.meiva per test.

import json
from pathlib import Path

import pytest

from cli.commands.extensions.mcp import cmd_mcp_add, cmd_mcp_disable, cmd_mcp_enable, cmd_mcp_list


def test_enable_then_disable_writes_overlay(unit_home: Path) -> None:
    from base.packages.plugins.mcp_enabled import read_enabled

    assert cmd_mcp_enable("foo", home=unit_home) == 0
    assert read_enabled(home=unit_home) == {"foo": True}

    assert cmd_mcp_disable("foo", home=unit_home) == 0
    assert read_enabled(home=unit_home) == {"foo": False}


def test_disable_filters_from_default_load(unit_home: Path) -> None:
    from meiva.mcps.config import load_mcp_config

    cmd_mcp_add("foo", None, "npx", ["-y", "server-foo"], [], home=unit_home)
    assert "foo" in load_mcp_config(home=unit_home)

    assert cmd_mcp_disable("foo", home=unit_home) == 0
    # meiva:note
    # The default (filtering) load no longer returns the server; `include_disabled` still does.
    assert "foo" not in load_mcp_config(home=unit_home)
    assert "foo" in load_mcp_config(include_disabled=True, home=unit_home)


def test_list_marks_disabled_server(unit_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cmd_mcp_add("foo", None, "npx", ["-y", "server-foo"], [], home=unit_home)
    cmd_mcp_disable("foo", home=unit_home)
    capsys.readouterr()

    assert cmd_mcp_list(home=unit_home) == 0
    out = capsys.readouterr().out
    assert "foo" in out
    assert "[disabled]" in out


def test_disable_hints_when_undefined(unit_home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cmd_mcp_disable("nope", home=unit_home) == 0
    out = capsys.readouterr().out
    assert "no server named 'nope' is currently defined" in out


def test_overlay_file_shape(unit_home: Path) -> None:
    from base.packages.plugins.mcp_enabled import local_config_path

    cmd_mcp_disable("foo", home=unit_home)
    data = json.loads(local_config_path(home=unit_home).read_text())
    assert data["mcp_servers"]["foo"]["enabled"] is False


@pytest.mark.parametrize("requires", [[], {"gpu": False}, {"display": "false"}])
def test_enable_rejects_invalid_requirements_without_writing(
    unit_home: Path, requires: object, capsys: pytest.CaptureFixture[str]
) -> None:
    from base.packages.plugins.mcp_enabled import local_config_path

    spec = json.dumps({"command": "server-foo", "requires": requires})
    assert cmd_mcp_add("foo", spec, None, [], [], home=unit_home) == 0
    assert cmd_mcp_disable("foo", home=unit_home) == 0
    before = local_config_path(home=unit_home).read_bytes()
    capsys.readouterr()

    assert cmd_mcp_enable("foo", home=unit_home) == 1
    output = capsys.readouterr()
    assert "requires" in output.err
    assert output.out == ""
    assert local_config_path(home=unit_home).read_bytes() == before


def test_disable_preserves_invalid_requirements_as_a_close_operation(unit_home: Path) -> None:
    from base.packages.plugins.mcp_enabled import read_enabled

    spec = {"command": "server-foo", "requires": {"gpu": "invalid"}}
    assert cmd_mcp_add("foo", json.dumps(spec), None, [], [], home=unit_home) == 0
    assert cmd_mcp_disable("foo", home=unit_home) == 0
    assert read_enabled(home=unit_home) == {"foo": False}
    assert json.loads((unit_home / "mcp.json").read_text())["mcpServers"]["foo"] == spec


# meiva:note
# Enable and list read a declaration through `validate_requirements`, which takes no `Host`: a
# `display` requirement is checked for its shape here and probed only where a server is started.
@pytest.mark.parametrize("requires", [None, {}, {"display": True}])
def test_enable_and_list_accept_valid_declarations(unit_home: Path, requires: object) -> None:
    from base.packages.plugins.mcp_enabled import read_enabled

    spec = json.dumps({"command": "server-foo", "requires": requires})
    assert cmd_mcp_add("foo", spec, None, [], [], home=unit_home) == 0
    assert cmd_mcp_enable("foo", home=unit_home) == 0
    assert read_enabled(home=unit_home) == {"foo": True}
    assert cmd_mcp_list(home=unit_home) == 0


@pytest.mark.parametrize("requires", [[], {"gpu": False}, {"display": "false"}])
def test_list_rejects_invalid_requirements_before_printing_inventory(
    unit_home: Path, requires: object, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = json.dumps({"command": "server-foo", "requires": requires})
    assert cmd_mcp_add("foo", spec, None, [], [], home=unit_home) == 0
    assert cmd_mcp_disable("foo", home=unit_home) == 0
    capsys.readouterr()

    assert cmd_mcp_list(home=unit_home) == 1
    output = capsys.readouterr()
    assert "requires" in output.err
    assert output.out == ""
