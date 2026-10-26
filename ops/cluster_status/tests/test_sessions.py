# meiva:note
# The session listing and the persistent-shell operations of a host, against real session
# backends.
#
# The shells are real login shells on real ptys served by the pty-sessions service the
# `pty_service` fixture starts under the test's home; a service session is a real supervised
# process. Nothing is replaced, so the names, the launch epochs and the kill verdicts are the
# ones the backends produce. What the listing means is decided in `listing` and `shell_plans`
# and is asserted on values in their own tests.

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import ShellKillMode
from base.clock import SYSTEM_TIMERS
from base.sessions.backend import PosixProcSessionBackend, get_shell_backend
from base.sessions.pty.tests.job_wait import wait_for_job
from base.sessions.pty.tests.pty_shells import new, output_until, shell_process, type_line
from ops.cluster_status import (
    ShellNotFoundError,
    agent_shell_sessions,
    capture_shell,
    kill_agent_shells,
    kill_shell,
)
from ops.cluster_status.sessions import collect_sessions, kill_with_verdict

pytestmark = pytest.mark.usefixtures("pty_service")


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def service_session(unit_home: Path) -> Iterator[str]:
    name = "meiva-ops-status-test"
    backend = PosixProcSessionBackend(SYSTEM_TIMERS)
    assert backend.new_session(name, "/bin/sleep 300", unit_home, env=dict(os.environ))
    try:
        yield name
    finally:
        backend.kill_session(name, graceful=False)


def test_both_backends_are_listed_for_this_cluster_only(
    unit_home: Path, service_session: str
) -> None:
    for name in (
        "meiva-agent-7-shell-0",
        "meiva-agent-7-shell-0-watcher",
        "meiva-agent-9",
        "other-stray",
    ):
        new(name, unit_home)

    rows = collect_sessions(
        service_backend=PosixProcSessionBackend(SYSTEM_TIMERS),
        shell_backend=get_shell_backend(),
        now=_now(),
    )

    assert [row.name for row in rows] == [
        "meiva-agent-7-shell-0",
        "meiva-agent-7-shell-0-watcher",
        service_session,
    ]
    assert all(row.created_at is not None and row.uptime_seconds >= 0 for row in rows)


def test_an_agents_shells_are_listed_by_id_with_their_names(unit_home: Path) -> None:
    for name in (
        "meiva-agent-7",
        "meiva-agent-7-shell-2-watcher",
        "meiva-agent-7-shell-0",
        "meiva-agent-7-shell-1-dev-server",
        "meiva-agent-12-shell-3",
        "meiva-restarter",
    ):
        new(name, unit_home)

    shells = agent_shell_sessions(7, now=_now())

    assert [(shell.id, shell.name) for shell in shells] == [
        (0, None),
        (1, "dev-server"),
        (2, "watcher"),
    ]
    assert all(shell.created_at is not None for shell in shells)
    assert agent_shell_sessions(99, now=_now()) == []


def test_a_capture_returns_the_output_of_the_resolved_shell(unit_home: Path) -> None:
    new("meiva-agent-42-shell-7-build", unit_home)
    type_line("meiva-agent-42-shell-7-build", "echo captured-marker")
    output_until("meiva-agent-42-shell-7-build", "captured-marker")

    name, lines, created_at, uptime = capture_shell(42, 7, 200, now=_now())

    assert name == "meiva-agent-42-shell-7-build"
    assert "captured-marker" in [line.strip() for line in lines]
    assert lines[0].strip() and lines[-1].strip()
    assert created_at is not None
    assert uptime >= 0


def test_a_capture_of_a_shell_that_is_not_there_is_not_found(unit_home: Path) -> None:
    new("meiva-agent-42-shell-3", unit_home)

    with pytest.raises(ShellNotFoundError, match="agent 42 has no live shell 99"):
        capture_shell(42, 99, now=_now())


def test_killing_an_idle_shell_reports_it_was_not_interrupted(unit_home: Path) -> None:
    new("meiva-agent-7-shell-3-build", unit_home)
    type_line("meiva-agent-7-shell-3-build", "echo idle-ready")
    output_until("meiva-agent-7-shell-3-build", "idle-ready")

    assert kill_shell(7, 3) == (ShellKillMode.KILLED, False, "build")
    assert not get_shell_backend().has_session("meiva-agent-7-shell-3-build")


def test_killing_a_shell_with_a_running_job_reports_it_was_interrupted(unit_home: Path) -> None:
    name = "meiva-agent-7-shell-4"
    new(name, unit_home)
    type_line(name, "echo busy-ready")
    output_until(name, "busy-ready")
    type_line(name, "sleep 300")
    wait_for_job(shell_process(name), ["sleep", "300"])

    assert kill_shell(7, 4) == (ShellKillMode.KILLED, True, None)
    assert not get_shell_backend().has_session(name)


def test_killing_a_shell_that_is_not_there_is_absent() -> None:
    assert kill_shell(7, 3) == (ShellKillMode.ABSENT, False, None)


def test_a_backend_without_kill_verdicts_is_assumed_to_have_cut_work_short(
    unit_home: Path, service_session: str
) -> None:
    ok, interrupted = kill_with_verdict(PosixProcSessionBackend(SYSTEM_TIMERS), service_session)

    assert (ok, interrupted) == (True, True)
    assert service_session not in PosixProcSessionBackend(SYSTEM_TIMERS).list_sessions("meiva-ops-")


def test_a_kill_all_takes_the_owners_shells_below_the_cutoff_and_nothing_else(
    unit_home: Path,
) -> None:
    from base.sessions.page_session import page_session_name

    page = page_session_name(7, "dash_board", 3)
    survivors = (
        page,
        "meiva-agent-7",
        "meiva-agent-7-shell-6-new-generation",
        "meiva-agent-7-shell-8-later",
        "meiva-agent-71-shell-0",
        "meiva-agent-12-shell-3",
        "meiva-restarter",
    )
    doomed = (
        "meiva-agent-7-shell-0",
        "meiva-agent-7-shell-2-watcher",
        "meiva-agent-7-shell-5-dev-server",
    )
    for name in (*doomed, *survivors):
        new(name, unit_home)

    assert kill_agent_shells(7, before_session_index=6) == [0, 2, 5]

    remaining = get_shell_backend().list_sessions()
    assert remaining == sorted(survivors)
    assert kill_agent_shells(99, before_session_index=6) == []
