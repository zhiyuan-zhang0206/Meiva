"""The names each core `meiva` namespace shows to the agent do not change by accident.

`help()`, SDK-expand and the metering wrappers all read the same list
(`agent_visible_names`). A helper that moves between modules, or a submodule that gains a
public name, must not widen what an agent sees. These literals are the surface as reviewed; a
deliberate change to a namespace edits its list here in the same PR.
"""

from __future__ import annotations

import inspect

import pytest

import meiva
from meiva import security
from meiva.sdk_surface.discovery import agent_visible_names

_SURFACE: dict[str, list[str]] = {
    "agents": [
        "AgentDirectoryPage",
        "AgentRow",
        "AgentStatus",
        "CancelResult",
        "Harness",
        "HarnessAttachment",
        "HarnessAvailability",
        "Machine",
        "Neighbor",
        "OpenTaskRow",
        "OpenTasksHint",
        "ProbedHarnessModel",
        "RestartResult",
        "ResurrectResult",
        "ShellSessionKillTiming",
        "ShellSessionsKill",
        "TerminateOutcome",
        "TerminateResult",
        "cancel",
        "commands",
        "compact",
        "get_ancestors",
        "get_last_message",
        "get_neighbors",
        "get_status",
        "harness_availability",
        "list_agents",
        "list_machines",
        "restart",
        "resurrect",
        "send_message",
        "spawn",
        "terminate",
    ],
    "self": [
        "AGENT_ID",
        "MACHINE_SPEC",
        "MachineSpec",
        "attach",
        "compact",
        "pause_heartbeat",
        "restart",
        "set_label",
        "terminate",
    ],
    "ui": ["ask", "close_question", "publish"],
    "watcher": ["at", "cron", "launch"],
    "web": ["ReasoningEffort", "SearchResult", "fetch", "search"],
    "skills": [],
    "files": ["append", "delete", "edit", "glob", "read", "write"],
    "cwd": ["get", "set"],
    "shell": ["SessionHandle", "run", "run_background", "sessions"],
    # Not a top-level namespace: the scan the other namespaces run their results through. Its
    # three names are still what `meiva.help` and the docs lint treat as its surface.
    "security": ["SecurityFindingEntry", "is_flagged", "scan_content"],
}

_TOP_LEVEL = [
    "agents",
    "attach",
    "cwd",
    "files",
    "help",
    "mcps",
    "self",
    "shell",
    "skills",
    "ui",
    "understand",
    "watcher",
    "web",
]


@pytest.mark.parametrize("namespace", sorted(_SURFACE))
def test_the_agent_visible_names_of_a_core_namespace_are_pinned(namespace: str) -> None:
    module = {
        "agents": meiva.agents,
        "self": meiva.self,
        "ui": meiva.ui,
        "watcher": meiva.watcher,
        "web": meiva.web,
        "skills": meiva.skills,
        "files": meiva.files,
        "cwd": meiva.cwd,
        "shell": meiva.shell,
        "security": security,
    }[namespace]
    assert agent_visible_names(module) == _SURFACE[namespace]


def test_the_top_level_namespaces_are_pinned() -> None:
    assert agent_visible_names(meiva) == _TOP_LEVEL


# meiva:note
# `meiva.context` stays importable for framework and plugin code, but neither `help(meiva)`
# nor `dir(meiva)` lists it or the connections it carries.
def test_the_connection_and_context_names_are_not_shown_to_the_agent() -> None:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        meiva.help()
    shown = buf.getvalue()
    for name in ("context", "DB", "REDIS"):
        assert name not in dir(meiva)
        assert f"meiva.{name}" not in shown
    assert "context.sql" not in shown


@pytest.mark.parametrize(
    ("function", "signature"),
    [
        (security.is_flagged, "(content: str) -> bool"),
        (security.scan_content, "(content: str, source: str = 'unknown') -> str"),
    ],
    ids=["is_flagged", "scan_content"],
)
def test_the_signatures_behind_the_visible_names_are_pinned(
    function: object, signature: str
) -> None:
    assert str(inspect.signature(function)) == signature  # pyright: ignore[reportArgumentType]


# meiva:note
# The servers come from the machine's mcp.json and the plugins' bundled .mcp.json, so the list is live; what is pinned is that nothing else is in it (no `servers` / `description` helper: `meiva.help(meiva.mcps)` is the listing).
def test_the_mcps_surface_is_the_configured_servers_alone() -> None:
    from meiva import mcps

    assert set(agent_visible_names(mcps)) == set(dir(mcps))
