"""`meiva.help(meiva.mcps)` renders the MCP server index and nothing more."""

import json
from pathlib import Path

import pytest

import meiva
from meiva.sdk_surface.tests.context_home import context_over_unit_home as context_over_unit_home


@pytest.fixture
def fake_config(unit_home: Path) -> Path:
    """Point meiva_home() to tmpdir; the machine config lives there."""
    return unit_home / "mcp.json"


@pytest.mark.usefixtures("sdk_model_owner")
def test_help_on_mcps_module_is_index_only(
    fake_config: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`meiva.help(meiva.mcps)` is an INDEX: each configured server as a name plus at
    most its one-liner, never its tool list or a tool's JSON Schema. Pinning it
    because the `# Capabilities` section is the prompt's single MCP index — a
    render that reached for tools would connect to every configured server and
    put every tool schema in front of the agent. Tools stay one
    `meiva.help(meiva.mcps.<server>)` away."""
    fake_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {"command": "x", "description": "Local filesystem"},
                    "github": {"command": "y"},
                }
            }
        ),
        encoding="utf-8",
    )
    meiva.help(meiva.mcps)
    out = capsys.readouterr().out
    assert "from . import fs" in out
    assert "from . import github" in out
    assert 'from . import fs\n"""Local filesystem"""' in out
    assert 'from . import github\n"""Tools of MCP server \'github\'."""' in out
    assert "def servers" not in out
    assert "def description" not in out
    # No tool-schema surface: the render must not have connected to a server.
    assert "inputSchema" not in out
    assert "**kwargs" not in out


# meiva:note
# A bad `description` in a server's configuration fails the render loudly instead of being listed bare.
@pytest.mark.usefixtures("sdk_model_owner")
def test_help_on_mcps_module_rejects_a_non_string_description(fake_config: Path) -> None:
    fake_config.write_text(
        json.dumps({"mcpServers": {"fs": {"command": "x", "description": 3}}}), encoding="utf-8"
    )
    with pytest.raises(TypeError, match="fs: description must be a string, got int"):
        meiva.help(meiva.mcps)
