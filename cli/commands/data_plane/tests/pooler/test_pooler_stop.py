# meiva:note
# The native PgBouncer stop owner against real poolers.
#
# A pooler with a transaction in flight keeps a safe shutdown waiting, which is the situation a
# stop has to be correct in: it records the exact native birth before the first signal, signals
# once, and never aborts the waiting transaction on a retry. The first group runs against a
# pooler in front of a real throwaway Postgres; the second against a bare pooler, where the
# files and processes around the signal are what is asserted.

from __future__ import annotations

import contextlib
import dataclasses
import json
import signal
import socket
import time
from collections.abc import Callable
from pathlib import Path

import psutil
import psycopg
import pytest

from base.cluster import port_preflight
from base.cluster.machine import MachineIdentity
from base.native_process.ownership import OwnedProcess
from base.telemetry import EventPipeline
from cli.commands.data_plane import maintenance_stop
from cli.commands.data_plane import pgbouncer as pooler
from cli.commands.data_plane._pooler_stop import OwnedPooler
from cli.commands.data_plane.config import DataPlaneConfig
from cli.commands.data_plane.pooler.stop_intent import NativeBirth, native_birth, render_intent
from cli.commands.data_plane.tests.native_processes import native_pooler as native_pooler
from cli.commands.data_plane.tests.native_processes import (
    pooler_spec,
    running_pooler,
    sleeping_child,
)
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime

_ABSENT_REDIS = "redis://127.0.0.1:1"


def pooler_plane(config: DataPlaneConfig) -> DataPlaneConfig:
    return dataclasses.replace(
        config,
        db_url="postgresql://meiva@127.0.0.1:12345/test",
        redis_url=_ABSENT_REDIS,
        redis_admin_password="",
    )


@pytest.mark.parametrize(
    ("preexisting_drain", "compensation_retry"), [(False, False), (False, True), (True, False)]
)
def test_repeated_normal_stop_preserves_waiting_transaction(
    native_pooler: tuple[OwnedPooler, str, str],
    preexisting_drain: bool,
    compensation_retry: bool,
    operator_pipeline: Callable[[], EventPipeline],
    data_plane_config: DataPlaneConfig,
    unit_home: Path,
) -> None:
    custodian, pooled, direct = native_pooler
    with psycopg.connect(pooled) as client:
        client.execute("INSERT INTO pooler_stop_receipt VALUES (42)")
        if preexisting_drain:
            custodian.identity.send_signal(signal.SIGINT)
            deadline = time.monotonic() + 5
            while port_preflight.require_listener(
                custodian.identity, custodian.port, required=False
            ):
                assert time.monotonic() < deadline, "private pooler did not begin draining"
                time.sleep(0.01)
        for _attempt in range(2):
            failure: RuntimeError | TimeoutError | None = None
            try:
                if _attempt and compensation_retry:
                    pooler.stop_pgbouncer()
                else:
                    maintenance_stop.stop(
                        pooler_plane(data_plane_config),
                        0.5,
                        home=unit_home,
                        producer=operator_pipeline,
                    )
            except (RuntimeError, TimeoutError) as exc:
                failure = exc
            assert custodian.identity.live(), "retry must not immediately abort the waiting pooler"
            assert client.execute("SELECT value FROM pooler_stop_receipt").fetchone() == (42,)
            assert failure is not None and "custody retained" in str(failure)
        client.commit()
    assert custodian.stop(deadline=time.monotonic() + 5)
    with psycopg.connect(direct) as conn:
        assert conn.execute("SELECT value FROM pooler_stop_receipt").fetchone() == (42,)
    assert not custodian.identity.live()


# meiva:note
# A busy host's socket scan is slow enough for a retained drain to finish while its listeners
# are scanned. That window is a race inside the scan itself, which no real resource opens on
# demand, so the scan is the one thing replaced: it finishes the drain (the client commits and
# the birth exits) and then reads the table. A gone birth leaves nothing to stop.
@pytest.mark.parametrize(("force", "budget"), [(False, 5.0), (True, -1.0)])
def test_a_drain_that_finishes_during_the_listener_scan_is_stopped(
    native_pooler: tuple[OwnedPooler, str, str],
    monkeypatch: pytest.MonkeyPatch,
    force: bool,
    budget: float,
) -> None:
    custodian, pooled, direct = native_pooler
    scan = port_preflight.strict_listeners_on
    with contextlib.closing(psycopg.connect(pooled)) as client:
        client.execute("INSERT INTO pooler_stop_receipt VALUES (7)")
        assert not custodian.stop(deadline=time.monotonic() + 2)

        def drain_finishes_during_scan(port: int) -> list[int]:
            client.commit()
            deadline = time.monotonic() + 5
            while custodian.identity.live():
                assert time.monotonic() < deadline, "private pooler did not finish its drain"
                time.sleep(0.01)
            return scan(port)

        monkeypatch.setattr(port_preflight, "strict_listeners_on", drain_finishes_during_scan)
        assert custodian.stop(deadline=time.monotonic() + budget, force=force)
    with psycopg.connect(direct) as conn:
        assert conn.execute("SELECT value FROM pooler_stop_receipt").fetchone() == (7,)
    assert not custodian.identity.live()


@pytest.mark.parametrize("force_budget", [0.2, -1.0])
def test_explicit_force_can_finish_a_retained_drain(
    native_pooler: tuple[OwnedPooler, str, str], force_budget: float
) -> None:
    custodian, pooled, direct = native_pooler
    with contextlib.closing(psycopg.connect(pooled)) as client:
        client.execute("INSERT INTO pooler_stop_receipt VALUES (99)")
        assert not custodian.stop(deadline=time.monotonic() + 2)
        assert custodian.identity.live()
        assert custodian.stop(deadline=time.monotonic() + force_budget, force=True)
        with pytest.raises(psycopg.OperationalError):
            client.execute("SELECT 1")
    with psycopg.connect(direct) as conn:
        assert conn.execute("SELECT count(*) FROM pooler_stop_receipt").fetchone() == (0,)


# meiva:note
# A restart whose safe shutdown does not finish in its bound (a client holds a transaction)
# keeps custody of the running pooler and fails the call; it never starts a second pooler on the
# same port nor kills the first underneath its transaction.
def test_a_restart_cannot_replace_a_pooler_holding_a_transaction(
    native_pooler: tuple[OwnedPooler, str, str],
    unit_home: Path,
    capsys: pytest.CaptureFixture[str],
    command_machine: MachineIdentity,
) -> None:
    custodian, pooled, direct = native_pooler
    with psycopg.connect(pooled) as client:
        client.execute("INSERT INTO pooler_stop_receipt VALUES (5)")
        # meiva:note
        # The hand-written fixture ini differs from the rendered one, so the running pooler is
        # a changed one and bring-up restarts it.
        spec = pooler_spec(
            command_machine,
            listen_port=custodian.port,
            socket_dir=unit_home,
            stop_wait_s=0.5,
        )

        assert pooler.ensure_pgbouncer(spec) == 1

        assert "could not stop the running pooler" in capsys.readouterr().err
        assert custodian.identity.live()
        assert client.execute("SELECT value FROM pooler_stop_receipt").fetchone() == (5,)
        client.commit()
    assert custodian.stop(deadline=time.monotonic() + 5)
    with psycopg.connect(direct) as conn:
        assert conn.execute("SELECT value FROM pooler_stop_receipt").fetchone() == (5,)


# meiva:note
# The stop records the birth before it signals, so a crash between the record and the signal
# leaves an intent for a pooler that was never told. A retry must never read that as licence to
# signal again: ambiguous delivery never authorizes a retry. The intent here is written by hand
# for the live pooler's birth, exactly as the crashed stop would have left it.
def test_a_recorded_intent_never_authorizes_another_signal(unit_home: Path) -> None:
    with running_pooler(unit_home) as running:
        custodian = OwnedPooler.from_config(running.identity, running.directory / "pgbouncer.ini")
        intent = running.directory / "stop-intent.json"
        intent.write_text(render_intent(_birth_of(running.identity)))

        with pytest.raises(RuntimeError, match="stop was already requested"):
            custodian.require_accepting()
        assert not custodian.stop(deadline=time.monotonic() + 0.3)

        assert running.identity.live(), "the pooler was never signalled"
        assert port_preflight.require_listener(running.identity, running.port)


def _birth_of(identity: OwnedProcess) -> NativeBirth:
    return native_birth(identity.pid, identity.birth, identity.starttime)


# meiva:note
# An intent recorded for an earlier birth of the same pid names no live process; it must not
# keep this birth from being stopped, and it is replaced by this birth's own.
def test_an_intent_for_an_earlier_birth_does_not_stop_this_one_from_being_stopped(
    unit_home: Path,
) -> None:
    with running_pooler(unit_home) as running:
        custodian = OwnedPooler.from_config(running.identity, running.directory / "pgbouncer.ini")
        intent = running.directory / "stop-intent.json"
        earlier = native_birth(
            running.identity.pid,
            running.identity.birth - 100,
            running.identity.starttime - 100 if running.identity.starttime is not None else None,
        )
        intent.write_text(render_intent(earlier))

        assert custodian.stop(deadline=time.monotonic() + 5)

        assert not running.identity.live()
        assert json.loads(intent.read_text()) == json.loads(
            render_intent(_birth_of(running.identity))
        )


# meiva:note
# A stop whose budget is already spent admits no signal: the intent is not even recorded, and
# the pooler keeps serving.
def test_an_expired_stop_budget_never_admits_a_signal(unit_home: Path) -> None:
    with running_pooler(unit_home) as running:
        custodian = OwnedPooler.from_config(running.identity, running.directory / "pgbouncer.ini")

        with pytest.raises(TimeoutError, match="stop deadline expired; custody retained"):
            custodian.stop(deadline=time.monotonic() - 1)

        assert running.identity.live()
        assert not (running.directory / "stop-intent.json").exists()
        assert port_preflight.require_listener(running.identity, running.port)


# meiva:note
# A pid that is no longer the recorded birth is none of the stop's business: the identity is
# a real running process whose start identity was altered, and the stop reports it gone
# without signalling it (SIGINT would end it).
def test_a_replaced_pid_is_reported_gone_and_not_signalled(tmp_path: Path) -> None:
    with sleeping_child() as child:
        real = OwnedProcess.capture(psutil.Process(child.pid))
        replaced = OwnedProcess(
            real.pid,
            real.birth + 1,
            real.starttime + 1 if real.starttime is not None else None,
        )
        custodian = OwnedPooler(replaced, 16433, tmp_path / "pgbouncer.ini")

        assert custodian.stop(deadline=time.monotonic() + 1)

        assert real.live(), "the process behind the replaced pid was not signalled"
        assert not (tmp_path / "stop-intent.json").exists()


# meiva:note
# A listener on the pooler's port that does not belong to the recorded process refuses before
# any intent is written or signal sent. The recorded process is a real child; the listener is
# this test's own socket.
def test_a_foreign_listener_refuses_before_intent_or_signal(tmp_path: Path) -> None:
    with sleeping_child() as child:
        recorded = OwnedProcess.capture(psutil.Process(child.pid))
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as foreign:
            foreign.bind(("127.0.0.1", 0))
            foreign.listen()
            custodian = OwnedPooler(recorded, foreign.getsockname()[1], tmp_path / "pgbouncer.ini")

            with pytest.raises(RuntimeError, match="does not belong"):
                custodian.stop(deadline=time.monotonic() + 1)

        assert child.poll() is None, "the recorded process was not signalled"
        assert not (tmp_path / "stop-intent.json").exists()


def test_a_corrupt_stop_intent_cannot_authorize_another_signal(unit_home: Path) -> None:
    with running_pooler(unit_home) as running:
        custodian = OwnedPooler.from_config(running.identity, running.directory / "pgbouncer.ini")
        (running.directory / "stop-intent.json").write_text(json.dumps({"pid": running.pid}))

        with pytest.raises(RuntimeError, match="invalid PgBouncer stop intent"):
            custodian.stop(deadline=time.monotonic() + 1)

        assert running.identity.live(), "the pooler was never signalled"
