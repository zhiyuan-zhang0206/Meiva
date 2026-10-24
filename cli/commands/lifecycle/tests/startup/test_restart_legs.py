# meiva:note
# `meiva restart` end to end: its probes, readiness gate, stop leg and start leg against a root.
#
# What each leg means for the next (`restart_plan`) and which units a stop touches
# (`reconcile_plan.plan_tree_stop`) are asserted on values. These tests run the whole command:
# the root is a real control socket (`scripted_root`) whose own record of the verbs it served is
# the evidence of what the restart did, the lifecycle journal is the real file under the process
# home, and the start leg publishes serving only through the suite's Postgres and a local gateway.

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.deploy.lifecycle import start_serving
from base.deploy.tests.containers import free_port
from base.host.db_authority.tests.generation import (
    seed_write_generation as seed_write_generation,
)
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root
from cli.commands.lifecycle.tests.startup.start_harness import (
    STATUS_PATH,
    StartRun,
    gateway_listener,
)
from cli.commands.lifecycle.tests.stop.stop_support import (
    REDIS_ADMIN_AUTH,
    cluster_redis,
    redis_answers,
)

pytestmark = pytest.mark.real_service_readiness_gate

RUNNER = frozenset({"agent-runner"})
GATEWAY = frozenset({"gateway"})


# meiva:note
# The verbs the root served, in order, without the read-only `status` polls.
def _served(calls: list[tuple[str, str | None]]) -> list[tuple[str, str | None]]:
    return [call for call in calls if call[0] != "status"]


def _journal(process_home: Path) -> dict[str, Any]:
    return json.loads((process_home / "run/lifecycle-op.json").read_text())


# meiva:note
# A runner whose tree runs pty-sessions, the service the restart selects, a service outside the
# selection and the browser, which the restart's stop leg keeps.
def _tree(run: StartRun) -> tuple[list[UnitRow], str]:
    generation = run.generation(RUNNER, run.roster(RUNNER, only=("pty-sessions",)))
    rows = [
        UnitRow(
            "pty-sessions",
            manifest_digest=generation.units["pty-sessions"],
        ),
        UnitRow("outside-the-selection"),
        UnitRow("browser"),
    ]
    return rows, generation.launch_digest


def test_a_restart_stops_the_tree_but_the_browser_and_brings_the_selection_back(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    run.command.config.set_field("browser_cdp_port", free_port())
    with gateway_listener() as gateway:
        run.declare(gateway=False, agent_runner=True, gateway_url=gateway.url)
        rows, launch_digest = _tree(run)

        with scripted_root(run.home, rows, launch_digest=launch_digest) as root:
            assert run.restart(only=("pty-sessions",)) == 0

    served = _served(root.calls)
    assert ("down", "browser") not in served, "the stop leg keeps the browser"
    assert [call for call in served if call[0] == "up"] == [("up", "pty-sessions")]
    stop_leg = served[: served.index(("up", "pty-sessions"))]
    assert set(stop_leg) == {("down", "outside-the-selection"), ("down", "pty-sessions")}
    assert stop_leg.index(("down", "outside-the-selection")) < stop_leg.index(
        ("down", "pty-sessions")
    ), "the pty-sessions unit goes down after the services it hosted shells for"
    assert {row.unit_id: row.state for row in rows} == {
        "pty-sessions": "running",
        "outside-the-selection": "stopped",
        "browser": "running",
    }
    assert gateway.requests == [STATUS_PATH] * 3, (
        "the gateway was dialled by the preflight probe before anything stopped, by the start "
        "leg's own probe and by the start leg's status block"
    )
    assert start_serving.is_serving(), "the start leg published serving again"
    journal = _journal(run.process_home)
    assert journal["complete"] and journal["result"] == {"rc": 0, "error": None}
    assert [phase["name"] for phase in journal["phases"]] == [
        "preflight",
        "preflight",
        "stop",
        "services",
        "shell-sessions",
        "pty-sessions",
        "start",
    ], "services first, then the shell sessions, then the pty-sessions unit itself, then the start"
    out = capsys.readouterr().out
    assert "[meiva restart]" in out and "preflight: probe gateway" in out


# meiva:note
# The restart's start leg waits for readiness within the budget the operator gave it, and its
# result is the restart's: a gate that the root accepts and that exits again at once never
# becomes ready, so the leg fails when its own `start_timeout` runs out, not the start's default
# wait, and the journal says the start leg is what failed. The gate is a core service: an
# optional one that misses its window is only reported. The browser stays up (the restart keeps
# it) so the root, which this test process stands in for, has a unit to host and is never asked to
# exit; the gateway host's own data plane is a real Redis the restart must leave running.
def test_the_start_leg_waits_only_as_long_as_the_operators_start_timeout(
    start_run: StartRun,
    tmp_path: Path,
    seed_write_generation: Callable[[Path], Any],
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = start_run
    seed_write_generation(run.home)
    run.command.config.set_field("browser_cdp_port", free_port())
    with cluster_redis(tmp_path / "redis", refuse_shutdown=False) as port:
        run.declare(gateway=True, agent_runner=False)
        run.command.config.set_field("redis_url", f"redis://127.0.0.1:{port}")
        run.command.config.set_field("redis_admin_password", REDIS_ADMIN_AUTH)
        generation = run.generation(GATEWAY, run.roster(GATEWAY, only=("gate",)))
        rows = [
            UnitRow("gate", manifest_digest=generation.units["gate"], stays_down=True),
            UnitRow("browser"),
        ]
        started = time.monotonic()
        with scripted_root(run.home, rows, launch_digest=generation.launch_digest) as root:
            rc = run.restart(only=("gate",), start_timeout=0.2)
        elapsed = time.monotonic() - started

    assert rc != 0
    assert elapsed < 30, "the start leg did not wait out the start's own default budget"
    served = _served(root.calls)
    assert served[-1] == ("up", "gate") and set(served[:-1]) == {("down", "gate")}
    journal = _journal(run.process_home)
    assert journal["complete"] and journal["result"] == {
        "rc": rc,
        "error": f"start leg failed with rc={rc}",
    }
    assert not start_serving.is_serving()
    err = capsys.readouterr().err
    assert "1 service(s) not ready" in err and "meiva-gate" in err
    assert "the 0s readiness bound" in err, "the wait was bounded by the operator's start timeout"


# meiva:note
# A restart runs its start in-process and never execs `.venv/bin/meiva`, so a checkout whose
# launcher is not executable does not stop it: the same checkout would refuse an update's start.
def test_a_launcher_the_restart_never_execs_does_not_refuse_it(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    run.command.config.set_field("browser_cdp_port", free_port())
    launcher = run.runtime.code_root / ".venv" / "bin" / "meiva"
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o644)
    with gateway_listener() as gateway:
        run.declare(gateway=False, agent_runner=True, gateway_url=gateway.url)
        rows, launch_digest = _tree(run)

        with scripted_root(run.home, rows, launch_digest=launch_digest) as root:
            assert run.restart(only=("pty-sessions",)) == 0

    assert [call for call in _served(root.calls) if call[0] == "up"] == [("up", "pty-sessions")]
    assert "launcher" not in capsys.readouterr().err


# meiva:note
# A unit with no persisted setup can neither register nor probe: the restart is declined before
# the stop leg, the live root is never asked to change anything, and the journal says why.
def test_a_restart_that_the_probes_decline_stops_nothing_and_leaves_the_root_alone(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    rows, launch_digest = _tree(run)

    with scripted_root(run.home, rows, launch_digest=launch_digest) as root:
        rc = run.restart(only=("pty-sessions",))

    assert rc != 0
    assert "refusing restart: preflight probes failed" in capsys.readouterr().err
    assert _served(root.calls) == [], "nothing was stopped, and nothing was started"
    assert {row.unit_id: row.state for row in rows} == {
        "pty-sessions": "running",
        "outside-the-selection": "running",
        "browser": "running",
    }
    journal = _journal(run.process_home)
    assert journal["complete"] and journal["result"] == {
        "rc": rc,
        "error": "preflight probes failed",
    }


# meiva:note
# A restart keeps the private data plane: its start leg needs the database. The cluster's own
# Redis here is a real instance that a stray `shutdown` would end.
def test_a_restart_on_a_gateway_host_leaves_its_data_plane_running(
    redis_access: None,
    start_run: StartRun,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = start_run
    run.command.config.set_field("browser_cdp_port", free_port())
    with cluster_redis(tmp_path / "redis", refuse_shutdown=False) as port:
        run.declare(gateway=True, agent_runner=False)
        run.command.config.set_field("redis_url", f"redis://127.0.0.1:{port}")
        run.command.config.set_field("redis_admin_password", REDIS_ADMIN_AUTH)
        generation = run.generation(GATEWAY, run.roster(GATEWAY, only=("pty-sessions",)))
        rows = [
            UnitRow("pty-sessions", manifest_digest=generation.units["pty-sessions"]),
            UnitRow("browser"),
        ]

        with scripted_root(run.home, rows, launch_digest=generation.launch_digest) as root:
            assert run.restart(only=("pty-sessions",)) == 0

        assert redis_answers(port), "the restart did not stop the cluster's own redis"
    assert _served(root.calls) == [("down", "pty-sessions"), ("up", "pty-sessions")]
    assert "stopping per-cluster data plane" not in capsys.readouterr().out
