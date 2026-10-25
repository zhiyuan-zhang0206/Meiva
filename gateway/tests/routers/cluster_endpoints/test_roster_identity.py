# meiva:note
# The gateway's cluster status gather over real `/ops` dials: the local probe, identity
# mismatch episodes, row order, the paused reason.

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

import gateway.cluster.status as status_mod
from base.api_contracts.status import MachineStatus
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.cluster.roster.roster_probe import IdentityMismatchLog
from gateway.cluster.roster.roster_rows import RosterRowSpec
from gateway.cluster.status import RosterEnv
from gateway.tests.support.runner_ops.ops_stub import OpsCall, OpsReply, completed, runner_ops


def _row(name: str, url: str | None, *roles: str, stopped: bool = False) -> RosterRowSpec:
    now = datetime.now(UTC)
    return RosterRowSpec(
        name=name,
        gateway_url=url,
        role=list(roles or ("agent-runner",)),
        up_since_at=now,
        description=None,
        stopped_at=now if stopped else None,
    )


def _env(
    database: Database, machine: MachineIdentityReader, home: Path, log: IdentityMismatchLog
) -> RosterEnv:
    return RosterEnv(
        db=database,
        identity_log=log,
        image=LOADED_IMAGE,
        home=home,
        machine=machine,
        probe_timeout_s=8.0,
    )


def _gather(env: RosterEnv, rows: list[RosterRowSpec]) -> list[MachineStatus]:
    return asyncio.run(status_mod.gather_cluster_status(env, rows, None))


def _answer(name: str, **extra: object) -> OpsReply:
    return completed(
        {
            "machine_name": name,
            "serve_gateway": True,
            "serve_agent_runner": True,
            "paused": False,
            "head_sha": "abc123",
            **extra,
        }
    )


# meiva:note
# A local agent-runner row is probed via its own ops server (status_probe, dialed at its
# registered URL), picking up shell_count + daemon health from the op result — same path as
# any remote machine.
def test_a_runner_row_is_probed_over_its_registered_url(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    reply = _answer(
        "m1",
        running_sha="def456",
        shell_count=4,
        agent_host_online=True,
        supervisor_online=False,
    )
    with runner_ops(db_conn, "m1", {"status_probe": reply}) as runner:
        [m] = _gather(
            _env(database, machine_identity, tmp_path, IdentityMismatchLog()),
            [_row("m1", runner.url, "agent-runner", "gateway")],
        )

    assert m.online is True
    assert m.identity_mismatch is False
    assert m.head_sha == "abc123"
    assert m.running_sha == "def456"
    assert m.shell_count == 4
    assert m.agent_host_online is True
    assert m.supervisor_online is False


# meiva:note
# If the ops server answers under a machine_name != the targeted row, the probe returns a
# loud identity-mismatch row (online False, identity_mismatch True) instead of a false-green
# online. Guards the 2026-07-18 incident where a loopback gateway_url made the gateway dial
# itself and answer under its own name.
def test_the_probe_flags_identity_mismatch_when_the_responder_name_differs(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with runner_ops(db_conn, "air", {"status_probe": _answer("gateway-host")}) as runner:
        [m] = _gather(
            _env(database, machine_identity, tmp_path, IdentityMismatchLog()),
            [_row("air", runner.url)],
        )

    assert m.name == "air"
    assert m.identity_mismatch is True
    assert m.online is False
    # meiva:note
    # it did NOT pick up the impostor's data
    assert m.head_sha is None


# meiva:note
# An active row's mismatch stays a loud ERROR, but one line per episode — not one per panel
# poll; a correct identity echo ends the episode, so a later mismatch logs anew (task #4143).
def test_an_identity_mismatch_logs_once_per_episode(
    caplog: pytest.LogCaptureFixture,
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    responder = {"name": "gateway-host"}

    def reply(call: OpsCall) -> OpsReply:
        del call
        return _answer(responder["name"])

    env = _env(database, machine_identity, tmp_path, IdentityMismatchLog())
    caplog.set_level(logging.DEBUG, logger="gateway.cluster.roster.roster_probe")

    def episode_records() -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.name == "gateway.cluster.roster.roster_probe"]

    with runner_ops(db_conn, "air", {"status_probe": reply}) as runner:
        rows = [_row("air", runner.url)]

        assert _gather(env, rows)[0].identity_mismatch is True
        assert [r.levelno for r in episode_records()] == [logging.ERROR]

        # meiva:note
        # A second poll of the same mismatch is silent.
        caplog.clear()
        _gather(env, rows)
        assert episode_records() == []

        # meiva:note
        # The identity echoes correctly -> the episode ends...
        caplog.clear()
        responder["name"] = "air"
        assert _gather(env, rows)[0].online is True

        # meiva:note
        # ...so the next mismatch is a fresh episode and logs again.
        responder["name"] = "gateway-host"
        _gather(env, rows)
        assert [r.levelno for r in episode_records()] == [logging.ERROR]


# meiva:note
# A stopped row's stale URL answering as another host is the expected face of the stop: the
# verdict stays (the row is still not that host), but the line degrades to INFO and is reported
# once per episode (task #4143).
def test_an_identity_mismatch_on_a_stopped_machine_is_info_once(
    caplog: pytest.LogCaptureFixture,
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    caplog.set_level(logging.DEBUG, logger="gateway.cluster.roster.roster_probe")
    env = _env(database, machine_identity, tmp_path, IdentityMismatchLog())

    with runner_ops(db_conn, "air", {"status_probe": _answer("gateway-host")}) as runner:
        rows = [_row("air", runner.url, stopped=True)]
        machines = _gather(env, rows)
        _gather(env, rows)

    assert machines[0].identity_mismatch is True
    records = [r for r in caplog.records if r.name == "gateway.cluster.roster.roster_probe"]
    assert [r.levelno for r in records] == [logging.INFO]
    assert "stopped machine" in records[0].getMessage()


# meiva:note
# A local machine WITHOUT agent-runner capability (pure gateway) runs no ops server: its
# row is a lightweight local read — no probe dispatched, agent-runner-only fields at their
# defaults, the checkout HEAD from the home's own `source`.
def test_a_pure_gateway_local_row_is_a_local_read_with_no_dial(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    name = machine_identity.name()
    with runner_ops(db_conn, "elsewhere", {}) as elsewhere:
        [m] = _gather(
            _env(database, machine_identity, tmp_path, IdentityMismatchLog()),
            [_row(name, elsewhere.url, "gateway")],
        )

    assert m.online is True
    # meiva:note
    # no serving root in a test home: the serving gate has not opened
    assert m.paused is True
    assert m.paused_reason == "startup"
    # meiva:note
    # the home has no `source` checkout
    assert m.head_sha is None
    assert m.running_sha == LOADED_IMAGE.sha
    assert m.shell_count == 0
    assert m.agent_host_online is None
    assert m.supervisor_online is None


# meiva:note
# The local pure-gateway row takes the lightweight local read and the address-less row is
# reported offline without a dial; the roster comes back ordered by machine name whatever
# order the table gave.
def test_the_gather_returns_rows_sorted_by_name(
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    rows = [
        _row("zz-runner", None),
        _row(machine_identity.name(), "http://m1", "gateway"),
        _row("aa-runner", None),
    ]

    machines = _gather(_env(database, machine_identity, tmp_path, IdentityMismatchLog()), rows)

    assert [m.name for m in machines] == sorted(["zz-runner", machine_identity.name(), "aa-runner"])
    assert [m.online for m in machines if m.name.endswith("-runner")] == [False, False]


# meiva:note
# A host parked by its serving gate must arrive on the roster with the breakdown attached
# (task #3404): paused=true + paused_reason='startup', not one opaque bool a consumer can
# misread as a deliberate pause.
def test_the_gather_carries_the_probe_paused_reason(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    reply = _answer("m1", paused=True, paused_reason="startup")
    with runner_ops(db_conn, "m1", {"status_probe": reply}) as runner:
        [m] = _gather(
            _env(database, machine_identity, tmp_path, IdentityMismatchLog()),
            [_row("m1", runner.url)],
        )

    assert m.online is True
    assert m.paused is True
    assert m.paused_reason == "startup"
