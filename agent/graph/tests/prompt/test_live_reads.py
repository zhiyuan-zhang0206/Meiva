# meiva:note
# The readers in `agent/graph/prompt/live.py` are the one place the prompt builder looks at the
# live `meiva` SDK; these tests run them against a per-test unit home with real skill folders,
# a real install registry and a real machine `mcp.json`.
from __future__ import annotations

import json

import pytest

from agent.graph.prompt.capabilities import McpServer
from agent.graph.prompt.live import read_mcp_servers, read_skills, read_view, skill_view
from agent.graph.tests.prompt.skill_support import write_skill
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.extensions import install_registry
from base.packages.extensions.install_registry import InstalledPackage
from base.paths import meiva_home, skills_dir

pytestmark = pytest.mark.usefixtures("unit_home")


def _install_skill(name: str) -> None:
    write_skill(skills_dir(meiva_home()), name, name, f"{name} desc")
    install_registry.register(InstalledPackage(name=name, type="skill"))


def test_installed_skills_are_read_and_unregistered_folders_are_not() -> None:
    _install_skill("alpha")
    write_skill(skills_dir(meiva_home()), "stray", "stray", "hand-copied")

    assert [s["name"] for s in read_skills(())] == ["alpha"]


def test_a_disabled_skills_surface_is_not_read() -> None:
    _install_skill("alpha")

    assert read_skills(("skills",)) == ()


def test_a_view_with_no_configured_lists_does_not_scan() -> None:
    _install_skill("alpha")

    view = read_view((), (), ())

    assert view.skills == ()


def test_the_view_carries_the_configured_lists_and_the_catalog() -> None:
    _install_skill("alpha")

    view = read_view(("*",), ("alpha",), ("watcher",))

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
        skill_view(prompt)
        skill_view(prompt)

    assert caplog.text.count("skills_to_inject_into_system_prompt: skill 'ghost' not found") == 2


def test_a_disabled_mcps_surface_is_not_read() -> None:
    assert read_mcp_servers(("mcps",)) == ()


def test_configured_servers_are_read_with_their_descriptions() -> None:
    config = {"mcpServers": {"alpha": {"command": "alpha-server", "description": "Alpha server"}}}
    (meiva_home() / "mcp.json").write_text(json.dumps(config), encoding="utf-8")

    assert McpServer("alpha", "Alpha server") in read_mcp_servers(())
