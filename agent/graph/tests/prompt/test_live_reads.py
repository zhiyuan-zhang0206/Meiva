# meiva:note
# The readers in `agent/graph/prompt/live.py` are the one place the prompt builder looks at the
# live `meiva` SDK; these tests run them against a per-test unit home with real skill folders,
# a real install registry and a real machine `mcp.json`.
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agent.graph.prompt.capabilities import McpServer
from agent.graph.prompt.live import read_mcp_servers, read_skills, read_view, skill_view
from agent.graph.tests.prompt.skill_support import write_skill
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.extensions import install_registry
from base.packages.extensions.install_registry import InstalledPackage
from base.paths import meiva_home, skills_dir
from meiva.sdk_surface.tests.context_home import context_over_unit_home as context_over_unit_home

ENVIRON = dict(os.environ)

pytestmark = pytest.mark.usefixtures("unit_home")


def _install_skill(name: str) -> None:
    write_skill(skills_dir(meiva_home()), name, name, f"{name} desc")
    install_registry.register(InstalledPackage(name=name, type="skill"), home=meiva_home())


def test_installed_skills_are_read_and_unregistered_folders_are_not() -> None:
    _install_skill("alpha")
    write_skill(skills_dir(meiva_home()), "stray", "stray", "hand-copied")

    assert [s["name"] for s in read_skills((), home=meiva_home(), environ=ENVIRON)] == ["alpha"]


def test_a_disabled_skills_surface_is_not_read() -> None:
    _install_skill("alpha")

    assert read_skills(("skills",), home=meiva_home(), environ=ENVIRON) == ()


def test_a_view_with_no_configured_lists_does_not_scan() -> None:
    _install_skill("alpha")

    view = read_view((), (), (), home=meiva_home(), environ=ENVIRON)

    assert view.skills == ()


def test_the_view_carries_the_configured_lists_and_the_catalog() -> None:
    _install_skill("alpha")

    view = read_view(("*",), ("alpha",), ("watcher",), home=meiva_home(), environ=ENVIRON)

    assert (view.inject, view.expand) == (("*",), ("alpha",))
    assert "watcher" in view.disabled
    assert [s["name"] for s in view.skills] == ["alpha"]


def test_unresolved_inject_names_warn_each_time_the_view_is_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_skill("alpha")
    prompt = AgentSlices.resolve(
        {"skills_to_inject_into_system_prompt": ["alpha", "ghost"]}, default_reader=defaults()
    ).prompt

    with caplog.at_level("WARNING", logger="agent.graph.prompt.live"):
        skill_view(prompt, home=meiva_home(), environ=ENVIRON)
        skill_view(prompt, home=meiva_home(), environ=ENVIRON)

    assert caplog.text.count("skills_to_inject_into_system_prompt: skill 'ghost' not found") == 2


def test_a_disabled_mcps_surface_is_not_read() -> None:
    assert read_mcp_servers(("mcps",), home=meiva_home()) == ()


def test_configured_servers_are_read_with_their_descriptions() -> None:
    config = {"mcpServers": {"alpha": {"command": "alpha-server", "description": "Alpha server"}}}
    (meiva_home() / "mcp.json").write_text(json.dumps(config), encoding="utf-8")

    assert McpServer("alpha", "Alpha server") in read_mcp_servers((), home=meiva_home())


# meiva:note
# The skill and MCP readers look under the home they are handed, not the process's own.
def test_the_readers_look_under_the_home_they_are_handed(tmp_path: Path) -> None:
    other = tmp_path / "other-home"
    write_skill(skills_dir(other), "alpha", "alpha", "alpha desc")
    install_registry.register(InstalledPackage(name="alpha", type="skill"), home=other)
    (other / "mcp.json").write_text(
        json.dumps({"mcpServers": {"fs": {"command": "x", "description": "files"}}}),
        encoding="utf-8",
    )

    assert [s["name"] for s in read_skills((), home=other, environ=ENVIRON)] == ["alpha"]
    assert read_skills((), home=meiva_home(), environ=ENVIRON) == ()
    handed = {m.name: m.description for m in read_mcp_servers((), home=other)}
    assert handed["fs"] == "files"
    assert "fs" not in {m.name for m in read_mcp_servers((), home=meiva_home())}
