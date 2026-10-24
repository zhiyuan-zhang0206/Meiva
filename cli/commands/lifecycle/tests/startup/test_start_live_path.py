# meiva:note
# `meiva start` over a live application root, end to end against real resources.
#
# A running root with the requested generation is only inspected: nothing converges, migrates or
# launches again, and the start reports readiness. What the plans decide is asserted in
# `../start_plans`; these tests show the shell dials the root, runs the plans in order, and
# publishes serving only after the readiness verdict. The root is a real control socket
# (`scripted_root`), the schema check and the machine registration use the suite's Postgres.

from __future__ import annotations

import psycopg
import pytest

from base.deploy.lifecycle import launch_failures, start_serving
from base.host.exit_codes import SERVICES_NOT_READY_EXIT_CODE
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root
from cli.commands.lifecycle.tests.startup.start_harness import (
    STATUS_PATH,
    StartRun,
    gateway_listener,
)

pytestmark = pytest.mark.real_service_readiness_gate

RUNNER = frozenset({"agent-runner"})
GATEWAY = frozenset({"gateway"})


# meiva:note
# The unit row of a root that runs `session` at the generation this checkout requests.
def _row(
    run: StartRun,
    roles: frozenset[str],
    session: str,
    *,
    state: str = "running",
    intent: str = "running",
    refuse_up: bool = False,
    begins_rival_start: bool = False,
) -> UnitRow:
    generation = run.generation(roles, run.roster(roles, only=(session,)))
    return UnitRow(
        session,
        state=state,
        intent=intent,
        manifest_digest=generation.units[session],
        refuse_up=refuse_up,
        begins_rival_start=begins_rival_start,
    )


def test_an_identical_live_generation_is_reused_and_the_start_serves(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    with gateway_listener() as gateway:
        run.declare(gateway=False, agent_runner=True, gateway_url=gateway.url)
        generation = run.generation(RUNNER, run.roster(RUNNER, only=("pty-sessions",)))
        row = _row(run, RUNNER, "pty-sessions")

        with scripted_root(run.home, [row], launch_digest=generation.launch_digest) as root:
            assert run.start(only=("pty-sessions",)) == 0

    assert start_serving.is_serving()
    assert gateway.requests, "a pure runner probes its gateway before launching"
    assert ("up", "pty-sessions") not in root.calls, "a running unit is not launched again"
    assert sorted(path.name for path in run.home.iterdir()) == ["run"], "nothing converged"
    out = capsys.readouterr().out
    assert "roles = agent-runner, machine = start-test" in out
    assert "probe gateway" in out


def test_a_gateway_host_does_not_probe_a_gateway_it_is(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    with gateway_listener() as gateway:
        run.declare(gateway=True, agent_runner=False, gateway_url=gateway.url)
        generation = run.generation(GATEWAY, run.roster(GATEWAY, only=("pty-sessions",)))
        row = _row(run, GATEWAY, "pty-sessions")

        with scripted_root(run.home, [row], launch_digest=generation.launch_digest):
            assert run.start(only=("pty-sessions",)) == 0

    assert "probe gateway" not in capsys.readouterr().out
    assert gateway.requests == [STATUS_PATH], "only the status block's own view of the gateway"
    assert start_serving.is_serving()


# meiva:note
# The gate answers no probe in this test: a core service that is not ready is the failure
# the start exits with, whatever else succeeded.
def test_an_unready_core_service_fails_the_start_and_it_never_serves(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    run.declare(gateway=True, agent_runner=False, remote_plane=True)
    generation = run.generation(GATEWAY, run.roster(GATEWAY, only=("gate",)))
    row = _row(run, GATEWAY, "gate")

    with scripted_root(run.home, [row], launch_digest=generation.launch_digest):
        assert run.start(only=("gate",)) == SERVICES_NOT_READY_EXIT_CODE

    assert not start_serving.is_serving()
    assert "meiva-gate" in capsys.readouterr().err


# meiva:note
# The rollout's local leg runs `meiva start` as a child, so the names of the core services
# that could not be launched travel in a file: an exit code cannot carry them.
def test_a_core_service_the_root_cannot_launch_fails_the_start_and_is_handed_to_the_rollout(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    run.declare(gateway=True, agent_runner=False, remote_plane=True)
    generation = run.generation(GATEWAY, run.roster(GATEWAY, only=("gate",)))
    row = _row(run, GATEWAY, "gate", state="stopped", intent="stopped", refuse_up=True)

    with scripted_root(run.home, [row], launch_digest=generation.launch_digest):
        assert run.start(only=("gate",)) == SERVICES_NOT_READY_EXIT_CODE

    assert not start_serving.is_serving()
    assert launch_failures.take() == ["meiva-gate"]
    assert "1 service(s) could not be launched" in capsys.readouterr().err


def test_an_optional_service_the_root_cannot_launch_is_reported_and_the_start_serves(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    with gateway_listener() as gateway:
        run.declare(gateway=False, agent_runner=True, gateway_url=gateway.url)
        generation = run.generation(RUNNER, run.roster(RUNNER, only=("pty-sessions",)))
        row = _row(run, RUNNER, "pty-sessions", state="stopped", intent="stopped", refuse_up=True)

        with scripted_root(run.home, [row], launch_digest=generation.launch_digest):
            assert run.start(only=("pty-sessions",)) == 0

    assert start_serving.is_serving()
    assert launch_failures.take() == []
    assert "pty-sessions" in capsys.readouterr().err


# meiva:note
# A restart leaves the live root unreconciled: units its stop kept still run, so a unit
# outside the selection is neither refused nor touched.
def test_the_start_asks_the_root_for_exactly_the_selection_it_is_given(start_run: StartRun) -> None:
    run = start_run
    with gateway_listener() as gateway:
        run.declare(gateway=False, agent_runner=True, gateway_url=gateway.url)
        generation = run.generation(RUNNER, run.roster(RUNNER, only=("pty-sessions",)))
        rows = [
            _row(run, RUNNER, "pty-sessions", state="stopped", intent="stopped"),
            UnitRow("kept-by-the-stop"),
        ]

        with scripted_root(run.home, rows, launch_digest=generation.launch_digest) as root:
            assert run.start(only=("pty-sessions",), reconcile=False) == 0

    assert [call for call in root.calls if call[0] != "status"] == [("up", "pty-sessions")]


# meiva:note
# Another start began while this one launched: only the attempt that owns the generation
# may publish serving.
def test_a_start_that_lost_its_serving_generation_never_reports_serving(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    with gateway_listener() as gateway:
        run.declare(gateway=False, agent_runner=True, gateway_url=gateway.url)
        generation = run.generation(RUNNER, run.roster(RUNNER, only=("pty-sessions",)))
        row = _row(
            run, RUNNER, "pty-sessions", state="stopped", intent="stopped", begins_rival_start=True
        )

        with scripted_root(run.home, [row], launch_digest=generation.launch_digest):
            assert run.start(only=("pty-sessions",)) == 1

    assert not start_serving.is_serving()
    assert "lost its serving generation" in capsys.readouterr().err


# meiva:note
# Changing a live root's inputs needs a prior stop: the start refuses before selection
# preparation, converge or any migration, and leaves the root as it found it.
def test_a_changed_live_generation_is_refused_before_anything_is_prepared(
    start_run: StartRun, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    with gateway_listener() as gateway:
        run.declare(gateway=False, agent_runner=True, gateway_url=gateway.url)
        row = _row(run, RUNNER, "pty-sessions")

        with scripted_root(run.home, [row], launch_digest="0" * 64) as root:
            assert run.start(only=("pty-sessions",)) == 1

    assert "start admission/preparation failed" in capsys.readouterr().err
    assert [call for call in root.calls if call[0] != "status"] == []
    assert sorted(path.name for path in run.home.iterdir()) == ["run"], "nothing converged"
    assert gateway.requests == [], "refused before the gateway probe"
    assert not start_serving.is_serving()


def test_a_selection_naming_no_such_service_launches_nothing(start_run: StartRun) -> None:
    run = start_run
    with gateway_listener() as gateway:
        run.declare(gateway=False, agent_runner=True, gateway_url=gateway.url)
        row = _row(run, RUNNER, "pty-sessions")

        with (
            scripted_root(run.home, [row]) as root,
            pytest.raises(ValueError, match="unknown service selection: typo"),
        ):
            run.start(only=("typo",))

    assert [call for call in root.calls if call[0] != "status"] == []
    assert not start_serving.is_serving()


# meiva:note
# A live generation is only inspected: pending schema changes need a stopped writer boundary
# and never run as a repeat-start effect.
def test_a_live_schema_that_does_not_match_the_code_refuses_without_applying_migrations(
    start_run: StartRun, db_conn: psycopg.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    run = start_run
    newest = db_conn.execute(
        "SELECT name FROM schema_migrations ORDER BY name DESC LIMIT 1"
    ).fetchone()
    assert newest is not None
    db_conn.execute("DELETE FROM schema_migrations WHERE name = %s", newest)
    db_conn.commit()
    try:
        with gateway_listener() as gateway:
            run.declare(gateway=False, agent_runner=True, gateway_url=gateway.url)
            generation = run.generation(RUNNER, run.roster(RUNNER, only=("pty-sessions",)))
            row = _row(run, RUNNER, "pty-sessions")

            with scripted_root(run.home, [row], launch_digest=generation.launch_digest) as root:
                assert run.start(only=("pty-sessions",)) == 1

        assert "DB is behind the code" in capsys.readouterr().err
        assert gateway.requests == [] and not start_serving.is_serving()
        assert [call for call in root.calls if call[0] != "status"] == []
        assert (
            db_conn.execute("SELECT 1 FROM schema_migrations WHERE name = %s", newest).fetchone()
            is None
        ), "a repeat start never applies a migration"
    finally:
        db_conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", newest)
        db_conn.commit()
