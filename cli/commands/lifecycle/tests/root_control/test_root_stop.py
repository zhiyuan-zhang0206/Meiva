# meiva:note
# The shell's stop of the application tree, against a real control socket and real files.
#
# Which units a stop takes down, in which order, is asserted on the plans (`test_reconcile_plan.py`);
# here the shell is shown to execute them against the scripted root.

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS, Deadline
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import root_manifests_path, root_run_dir
from cli.commands.lifecycle import root_driver as driver
from cli.commands.lifecycle.root_control.launch_plan import RootLaunch, plan_root_launch
from cli.commands.lifecycle.tests.root_control.support import LAUNCHD_FACTS, refuse_launchctl
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root

# meiva:note
# The caller block carries this test process's own pid: the value travels through the driver, so
# the assertion is about it reaching the wire, not about the driver reading it.
_PID = os.getpid()

pytestmark = pytest.mark.real_service_readiness_gate

ROLES = frozenset({"gateway"})


def launch_of(home: Path, interpreter: Path) -> RootLaunch:
    return plan_root_launch(
        interpreter=interpreter,
        cwd=home,
        run_dir=root_run_dir(home),
        manifests=root_manifests_path(home),
    )


# meiva:note
# ── stopping the tree ────────────────────────────────────────────────────────


def stop_tree(
    home: Path, rows: list[UnitRow], *, preserve: frozenset[str], force: bool
) -> dict[str, str]:
    with scripted_root(home, rows) as root:
        driver.stop_root_service_tree(
            preserve=preserve,
            timeout_s=10.0,
            force=force,
            helper=None,
            home=home,
            facts=LAUNCHD_FACTS,
            run=refuse_launchctl,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
            pid=_PID,
        )
    return {unit_id: row.state for unit_id, row in root.units.items()}


def test_a_stop_that_preserves_a_unit_takes_down_the_others_and_leaves_the_root(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [UnitRow("agent-host"), UnitRow("gateway"), UnitRow("pty-sessions")]

    states = stop_tree(short_tmp, rows, preserve=frozenset({"pty-sessions"}), force=False)

    assert states == {"agent-host": "stopped", "gateway": "stopped", "pty-sessions": "running"}
    assert "meiva-gateway stopped" in capsys.readouterr().out


def test_a_stop_that_preserves_every_unit_changes_nothing(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    states = stop_tree(
        short_tmp, [UnitRow("gateway")], preserve=frozenset({"gateway"}), force=False
    )

    assert states == {"gateway": "running"}
    assert "every unit is preserved" in capsys.readouterr().out


def test_a_forced_stop_force_downs_every_unit(short_tmp: Path) -> None:
    with scripted_root(short_tmp, [UnitRow("gateway"), UnitRow("ops")]) as root:
        driver.stop_root_service_tree(
            preserve=frozenset({"ops"}),
            timeout_s=10.0,
            force=True,
            helper=None,
            home=short_tmp,
            facts=LAUNCHD_FACTS,
            run=refuse_launchctl,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
            pid=_PID,
        )

    assert [verb for verb, _ in root.calls if verb != "status"] == ["force-down"]


def test_stopping_a_home_with_no_root_reports_no_custody(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    driver.stop_root_service_tree(
        preserve=frozenset(),
        timeout_s=1.0,
        force=False,
        helper=None,
        home=short_tmp,
        facts=LAUNCHD_FACTS,
        run=refuse_launchctl,
        timers=SYSTEM_TIMERS,
        environ=os.environ,
        pid=_PID,
    )

    assert "no live owner or retained service custody" in capsys.readouterr().out


def test_the_stop_plan_names_the_units_the_root_runs_that_are_not_preserved(
    short_tmp: Path,
) -> None:
    with scripted_root(short_tmp, [UnitRow("gateway"), UnitRow("ops")]):
        names = driver.root_tree_plan(
            frozenset({"ops"}), home=short_tmp, environ=os.environ, pid=_PID
        )

    assert names == ["meiva-gateway"]


def test_the_stop_plan_of_a_home_with_no_root_is_empty(short_tmp: Path) -> None:
    assert driver.root_tree_plan(frozenset(), home=short_tmp, environ=os.environ, pid=_PID) == []


def test_no_unit_goes_down_before_the_agent_host_has_exited(short_tmp: Path) -> None:
    # meiva:note
    # A root answers `down` only once the unit has exited, so nothing else is stopped while
    # the agent-host drains.
    order = ["agent-host", "gateway", "alpha-sidecar", "ops"]
    rows = [
        UnitRow(unit_id, exit_delay_s=0.2 if unit_id == "agent-host" else 0.0) for unit_id in order
    ]

    with scripted_root(short_tmp, rows) as root:
        driver.down_units(
            order,
            deadline=Deadline(at=time.monotonic() + 30, timers=SYSTEM_TIMERS),
            force=False,
            home=short_tmp,
            environ=os.environ,
            pid=_PID,
        )

    names = [name for name, _ in root.events]
    assert names[:2] == ["down agent-host", "exited agent-host"]
    assert [name for name in names if name.startswith("down ")] == [f"down {u}" for u in order]
    assert all("force-down" not in name for name in names)


def test_a_stop_whose_deadline_has_passed_takes_nothing_down(short_tmp: Path) -> None:
    with (
        scripted_root(short_tmp, [UnitRow("gateway")]) as root,
        pytest.raises(driver.RootDriverError, match="deadline expired; custody retained"),
    ):
        driver.down_units(
            ["gateway"],
            deadline=Deadline(at=time.monotonic() - 1, timers=SYSTEM_TIMERS),
            force=False,
            home=short_tmp,
            environ=os.environ,
            pid=_PID,
        )

    assert root.units["gateway"].state == "running"


def test_tiered_stop_drains_agent_host_first_then_stops_slow_siblings_in_parallel(
    short_tmp: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [
        UnitRow("agent-host", stop_tier=0, exit_delay_s=0.15),
        UnitRow("gateway", stop_tier=1, exit_delay_s=0.3),
        UnitRow("browser-mcp", stop_tier=1, exit_delay_s=0.3),
        UnitRow("ops", stop_tier=1, exit_delay_s=0.3),
        UnitRow("browser", stop_tier=2, exit_delay_s=0.05),
        UnitRow("pty-sessions", stop_tier=1),
    ]

    with scripted_root(short_tmp, rows) as root:
        driver.stop_root_service_tree(
            preserve=frozenset({"pty-sessions"}),
            timeout_s=10.0,
            force=False,
            helper=None,
            home=short_tmp,
            facts=LAUNCHD_FACTS,
            run=refuse_launchctl,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
            pid=_PID,
        )

    events = root.events
    names = [name for name, _ in events]
    assert names[:2] == ["down agent-host", "exited agent-host"]
    assert set(names[2:5]) == {"down gateway", "down browser-mcp", "down ops"}
    assert set(names[5:8]) == {"exited gateway", "exited browser-mcp", "exited ops"}
    assert names[8:] == ["down browser", "exited browser"]
    tier_one_began = events[1][1]
    tier_one_ended = events[-2][1]
    assert tier_one_ended - tier_one_began < 0.55, (
        f"expected parallel <0.55s tier-1 wave (serial sum 0.9s), took {tier_one_ended - tier_one_began:.3f}s"
    )
    out_lines = [line.strip() for line in capsys.readouterr().out.splitlines() if "stopped" in line]
    assert out_lines == [
        "✓ meiva-root unit meiva-agent-host stopped",
        "✓ meiva-root unit meiva-gateway stopped",
        "✓ meiva-root unit meiva-browser-mcp stopped",
        "✓ meiva-root unit meiva-ops stopped",
        "✓ meiva-root unit meiva-browser stopped",
    ]
