# meiva:note
# A stop's dealings with the root that owns the service tree, over a real control socket.
#
# The root is a listener under a short home that speaks the K1 wire and keeps a unit table; what a
# stop asked of it is what it served. Which units a stop keeps and which verb it uses are asserted
# on the plan; here the shell is shown to turn that plan into exactly those requests, within the
# operator's budget.

from __future__ import annotations

import io
import os
import plistlib
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import psycopg
import pytest

from base.daemon.health import DaemonProbe
from base.host.system.tests.fake_commands import DARWIN_ARM64, FakeCommands, Reply, fake_host
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.native_process.root_control.client import RootClientError
from base.telemetry import EventPipeline
from cli.commands.lifecycle import root_driver
from cli.commands.lifecycle.stop import do_stop
from cli.commands.lifecycle.stop.extras import stop_permissions_helper
from cli.commands.lifecycle.stop.restart import (
    admit_agent_host_restart,
    cmd_restart_agent_host,
    restart_agent_host,
)
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root
from cli.commands.lifecycle.tests.stop.stop_support import (
    stop_environment,
    stop_request,
)
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.database.tests.lifecycle_rows import LifecycleRows
from cli.database.tests.lifecycle_rows import lifecycle_rows as lifecycle_rows
from services.desktop.permissions_helper import launchd_job as jobs
from services.supervision.roster.service_spec import ServiceSpec

# meiva:note
# The readiness wait of `restart --only agent-host` is a subject here, not the guard that stubs it.
pytestmark = pytest.mark.real_service_readiness_gate

TREE = ("agent-host", "frontend", "browser", "pty-sessions")


@pytest.fixture
def rooted(command_runtime: CommandRuntime, short_tmp: Path) -> CommandRuntime:
    """An invocation of a unit whose root answers at `short_tmp` (a unix socket needs a short path)."""
    command_runtime.machine.set(role="agent-runner")
    return replace(command_runtime, home=short_tmp, helper=None)


# meiva:note
# What the stop asked the root to change, in order (its status reads left out).
def verbs(root_calls: Sequence[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    return [call for call in root_calls if call[0] != "status"]


def units(*, slow: tuple[str, float] | None = None) -> list[UnitRow]:
    return [
        UnitRow(unit, exit_delay_s=slow[1] if slow is not None and slow[0] == unit else 0.0)
        for unit in TREE
    ]


def test_a_stop_downs_the_services_then_the_pty_sessions_unit_and_leaves_what_it_keeps(
    rooted: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    # meiva:note
    # No pty-sessions service listens, so the shell-session closure has nothing to close: the
    # stop meets only the root. The kept `frontend` is never asked to stop, and the root stays
    # up to host it.
    with scripted_root(rooted.home, units()) as root:
        rc = do_stop(
            stop_request(preserve=frozenset({"frontend"}), keep_browser=True),
            stop_environment(rooted, operator_database, operator_pipeline),
            retained_children=[],
        )
    assert rc == 0
    asked = verbs(root.calls)
    assert asked[0] == ("down", "agent-host"), "the services phase comes first"
    assert asked[-1] == ("down", "pty-sessions"), "the pty-sessions unit goes down last"
    assert {unit for _verb, unit in asked} == {"agent-host", "browser", "pty-sessions"}
    assert [row.state for row in root.units.values()] == [
        "stopped",
        "running",
        "stopped",
        "stopped",
    ]


def test_keeping_the_shell_sessions_keeps_their_service_running_in_the_tree(
    rooted: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    with scripted_root(rooted.home, units()) as root:
        rc = do_stop(
            stop_request(keep_shell_sessions=True, preserve=frozenset({"frontend"})),
            stop_environment(rooted, operator_database, operator_pipeline),
            retained_children=[],
        )
    assert rc == 0
    assert verbs(root.calls) == [("down", "agent-host"), ("down", "browser")]
    assert root.units["pty-sessions"].state == "running"


def test_an_explicit_force_asks_the_root_for_the_interrupting_verb_on_both_stops(
    rooted: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    with scripted_root(rooted.home, units()) as root:
        rc = do_stop(
            stop_request(force=True, preserve=frozenset({"frontend"})),
            stop_environment(rooted, operator_database, operator_pipeline),
            retained_children=[],
        )
    assert rc == 0
    asked = verbs(root.calls)
    assert {verb for verb, _unit in asked} == {"force-down"}, "force never asks for a plain down"
    assert (asked[0], asked[-1]) == (("force-down", "agent-host"), ("force-down", "pty-sessions"))


# meiva:note
# A root whose unit outlives the operator's budget fails the stop, reported with its phase;
# nothing escalates to force behind the operator's back.
def test_a_normal_stop_never_uses_the_interrupting_verb_when_the_root_is_slow(
    rooted: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
    capsys: pytest.CaptureFixture[str],
) -> None:
    started = time.monotonic()
    with scripted_root(rooted.home, units(slow=("agent-host", 3.0))) as root:
        rc = do_stop(
            stop_request(timeout=0.5, preserve=frozenset({"frontend"})),
            stop_environment(rooted, operator_database, operator_pipeline),
            retained_children=[],
        )
    assert rc == 1
    assert time.monotonic() - started < 2.5, "the root's stop spends the operator's budget"
    err = capsys.readouterr().err
    assert "Stop incomplete" in err and "were not force-killed" in err
    assert "phases: services" in err
    assert ("force-down", "agent-host") not in root.calls


def test_a_force_stop_spends_the_operators_budget_on_the_root_as_well(
    rooted: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    started = time.monotonic()
    # meiva:note
    # A force stop has no journal to report into: the root's failure is the command's.
    with (
        scripted_root(rooted.home, units(slow=("agent-host", 3.0))),
        pytest.raises(RootClientError),
    ):
        do_stop(
            stop_request(force=True, timeout=0.5, preserve=frozenset({"frontend"})),
            stop_environment(rooted, operator_database, operator_pipeline),
            retained_children=[],
        )
    assert time.monotonic() - started < 2.5


@pytest.mark.parametrize(("answer", "stops"), [("y\n", True), ("n\n", False), ("", False)])
def test_a_force_stop_lists_the_live_service_tree_before_it_asks_and_stops_only_on_a_yes(
    answer: str,
    stops: bool,
    rooted: CommandRuntime,
    capsys: pytest.CaptureFixture[str],
    operator_database: OperatorDatabaseFactory,
    operator_pipeline: Callable[[], EventPipeline],
) -> None:
    with scripted_root(rooted.home, units()) as root:
        rc = do_stop(
            stop_request(
                force=True,
                require_confirmation=True,
                keep_browser=True,
                preserve=frozenset({"frontend"}),
            ),
            stop_environment(
                rooted, operator_database, operator_pipeline, stdin=io.StringIO(answer)
            ),
            retained_children=[],
        )
    assert rc == 0
    out = capsys.readouterr().out
    assert "The following will be stopped:" in out
    # meiva:note
    # The units the stop will down, by their session names; what it keeps is left out.
    assert "service sessions: meiva-agent-host, meiva-browser, meiva-pty-sessions" in out
    assert "browser: kept up (meiva-browser, login session preserved)" in out
    assert ("confirm? [y/N] " in out) and (("aborted" in out) is not stops)
    assert (len(verbs(root.calls)) > 0) is stops


# meiva:note
# ── `meiva restart --only agent-host` ────────────────────────────────────────


def agent_host(probe: Callable[[], DaemonProbe]) -> ServiceSpec:
    return ServiceSpec(
        session="agent-host",
        cmd="python -m agent_host",
        capabilities=frozenset({"agent-runner"}),
        requires_db=False,
        identity_probe=probe,
    )


def ready() -> DaemonProbe:
    return DaemonProbe.up("ready")


def unready() -> DaemonProbe:
    return DaemonProbe.down("unready")


# meiva:note
# The restart as the command composes it: admit the live root, then down and up through it with
# the agent-host spec a test chose (its readiness probe is the subject of two of the cases).
def restart(
    runtime: CommandRuntime, probe: Callable[[], DaemonProbe], *, force: bool = False
) -> int:
    client = admit_agent_host_restart(runtime, timeout=12.0)
    assert client is not None
    return restart_agent_host(
        runtime,
        client,
        agent_host(probe),
        force=force,
        timeout=12.0,
        start_timeout=0.2,
        database_factory=None,
    )


@pytest.mark.parametrize(("force", "verb"), [(False, "down"), (True, "force-down")])
def test_restart_agent_host_downs_then_ups_it_with_the_chosen_interruption(
    rooted: CommandRuntime, lifecycle_rows: LifecycleRows, force: bool, verb: str
) -> None:
    with scripted_root(rooted.home, [UnitRow("agent-host"), UnitRow("gateway")]) as root:
        rc = restart(rooted, ready, force=force)
    assert rc == 0
    assert verbs(root.calls) == [(verb, "agent-host"), ("up", "agent-host")]
    assert lifecycle_rows.requests() == [(verb, ["agent-host"]), ("up", ["agent-host"])], (
        "each request is named before it leaves"
    )
    assert root.units["agent-host"].state == "running" and root.units["gateway"].state == "running"


def test_restart_agent_host_without_a_live_root_changes_nothing(
    rooted: CommandRuntime, lifecycle_rows: LifecycleRows, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admit_agent_host_restart(rooted, timeout=12.0) is None
    assert lifecycle_rows.requests() == []
    assert "no live meiva-root on this unit" in capsys.readouterr().err


def test_restart_agent_host_refuses_a_root_that_runs_no_agent_host(
    rooted: CommandRuntime, lifecycle_rows: LifecycleRows, capsys: pytest.CaptureFixture[str]
) -> None:
    with scripted_root(rooted.home, [UnitRow("gateway")]) as root:
        assert admit_agent_host_restart(rooted, timeout=12.0) is None
    assert verbs(root.calls) == [] and lifecycle_rows.requests() == []
    assert "runs no agent-host unit" in capsys.readouterr().err


def test_restart_agent_host_fails_when_it_does_not_become_ready(
    rooted: CommandRuntime, capsys: pytest.CaptureFixture[str]
) -> None:
    with scripted_root(rooted.home, [UnitRow("agent-host")]) as root:
        assert restart(rooted, unready) == 1
    assert verbs(root.calls) == [("down", "agent-host"), ("up", "agent-host")]
    assert "agent-host did not become ready after restart" in capsys.readouterr().err


def test_restart_agent_host_raises_when_the_root_refuses_the_up_and_never_waits(
    rooted: CommandRuntime,
) -> None:
    with (
        scripted_root(rooted.home, [UnitRow("agent-host", refuse_up=True)]) as root,
        pytest.raises(root_driver.RootDriverError, match="up agent-host failed"),
    ):
        restart(rooted, ready)
    assert verbs(root.calls) == [("down", "agent-host"), ("up", "agent-host")]


# meiva:note
# The command end to end with the roster's own agent-host spec: nothing answers its health port
# here, so the restart brings the unit up through the root and then reports it never became ready.
def test_the_command_restarts_agent_host_through_the_root_and_reports_a_unit_that_never_answers(
    rooted: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    db_conn: psycopg.Connection,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with scripted_root(rooted.home, [UnitRow("agent-host")]) as root:
        rc = cmd_restart_agent_host(
            force=False,
            timeout=12.0,
            start_timeout=0.2,
            database_factory=operator_database,
            command=rooted,
        )
    assert rc == 1
    assert verbs(root.calls) == [("down", "agent-host"), ("up", "agent-host")]
    assert "agent-host did not become ready after restart" in capsys.readouterr().err
    # meiva:note
    # The command audited itself: its two requests and its failed outcome are rows of this test's
    # database.
    recorded = db_conn.execute(
        "SELECT event_name, attributes->>'verb', attributes->>'outcome' FROM audit_events "
        "WHERE event_name IN ('lifecycle_request', 'lifecycle_result') ORDER BY id"
    ).fetchall()
    assert recorded == [
        ("lifecycle_request", "down", None),
        ("lifecycle_request", "up", None),
        ("lifecycle_result", "up", "failed"),
    ]


def test_the_command_without_a_live_root_says_so_and_exits_nonzero(
    rooted: CommandRuntime,
    operator_database: OperatorDatabaseFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cmd_restart_agent_host(
        force=False,
        timeout=12.0,
        start_timeout=0.2,
        database_factory=operator_database,
        command=rooted,
    )
    assert rc == 1
    assert "no live meiva-root on this unit" in capsys.readouterr().err


# meiva:note
# `meiva stop` retires the home's helper as the process it runs in: a helper whose process is the
# stopping command itself is not unloaded from under it. The command's process is a real child
# here, the helper's job points at that child, and launchd's answer comes from the host the test
# hands in.
def test_the_stop_refuses_to_retire_the_helper_it_runs_under(
    command_runtime: CommandRuntime, tmp_path: Path
) -> None:
    home, user_home = tmp_path.resolve() / "home", tmp_path.resolve() / "user"
    sleeper = str(Path("/bin/sleep").resolve())
    child = subprocess.Popen([sleeper, "120"])
    try:
        commands = FakeCommands()
        commands.on(("launchctl", "print"), Reply(stdout=f"pid = {child.pid}\n"), repeat=True)
        host = fake_host(commands, platform=DARWIN_ARM64, uid=os.getuid())
        runtime = replace(
            command_runtime,
            home=home,
            host=host,
            pid=child.pid,
            session=UserSession(user_home=user_home, login_shell="", interactive=False),
        )
        port = runtime.config.view.services.permissions_helper_port
        plist = jobs.helper_job_plist_path(
            home, facts=jobs.LaunchdFacts.of(host, user_home=user_home)
        )
        plist.parent.mkdir(parents=True)
        plist.write_bytes(
            plistlib.dumps(
                {
                    "Label": jobs.helper_job_label(home),
                    "KeepAlive": {"SuccessfulExit": False},
                    "ProgramArguments": [sleeper],
                    "EnvironmentVariables": {
                        "MEIVA_PERMISSIONS_HELPER_SOCKET": str(
                            home / f"run/permissions-helper.{port}.sock"
                        ),
                        "MEIVA_PERMISSIONS_HELPER_ROOT_SEED": str(
                            home / "run/meiva-root/seed.json"
                        ),
                    },
                }
            )
        )

        with pytest.raises(RuntimeError, match="ancestor"):
            stop_permissions_helper(runtime, timeout_s=10.0)
        assert plist.exists()
    finally:
        child.kill()
        child.wait(timeout=5)
