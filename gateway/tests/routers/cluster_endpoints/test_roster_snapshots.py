# meiva:note
# The roster's read model: a machine renders from the heartbeat liveness pass's last
# probe, nothing is dialed for it, and only an explicit fresh read (or a machine with no
# fresh snapshot) dials. The dial is a real `/ops` round trip to a runner served on port 0.

from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

import gateway.cluster.status as status_mod
from base.api_contracts.status import MachineStatus
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from gateway.cluster.roster.liveness_snapshot import Snapshot
from gateway.cluster.roster.roster_rows import RosterRowSpec
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.routers.cluster_endpoints.cluster_support import roster_env
from gateway.tests.support.runner_ops.ops_stub import (
    OpsCall,
    OpsReply,
    Responder,
    RunnerOps,
    completed,
    refused,
    runner_ops,
)

_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _row(name: str, url: str | None) -> RosterRowSpec:
    return RosterRowSpec(
        name=name,
        gateway_url=url,
        role=["agent-runner"],
        up_since_at=_NOW,
        description=None,
        stopped_at=None,
    )


def _status(name: str = "runner-a", **extra: object) -> dict[str, object]:
    return {
        "machine_name": name,
        "serve_gateway": False,
        "serve_agent_runner": True,
        "paused": False,
        "head_sha": "abc123",
        "running_sha": "def456",
        "shell_count": 3,
        **extra,
    }


def _snap(
    *,
    reachable: bool = True,
    failures: int = 0,
    status: dict[str, object] | None = None,
    age_s: float = 5.0,
    status_age_s: float | None = None,
) -> Snapshot:
    now = datetime.now(UTC)
    status_at = None if status is None else now - timedelta(seconds=status_age_s or age_s)
    return Snapshot(
        observed_at=now - timedelta(seconds=age_s),
        reachable=reachable,
        consecutive_failures=failures,
        status=status,
        status_at=status_at,
    )


def _gather(
    database: Database,
    machine: MachineIdentityReader,
    home: Path,
    rows: list[RosterRowSpec],
    snaps: dict[str, Snapshot] | None,
    *,
    probe_timeout_s: float = 8.0,
) -> list[MachineStatus]:
    env = roster_env(database, machine, home=home, probe_timeout_s=probe_timeout_s)
    return asyncio.run(status_mod.gather_cluster_status(env, rows, snaps))


def _dialed(*runners: RunnerOps) -> list[str]:
    return sorted(runner.name for runner in runners if runner.calls_of("status_probe"))


# --- rendering from a snapshot ------------------------------------------------


def test_a_fresh_reachable_snapshot_renders_online_with_its_age(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with runner_ops(
        db_conn, "runner-a", {"status_probe": completed(_status("runner-a"))}
    ) as runner:
        [machine] = _gather(
            database,
            machine_identity,
            tmp_path,
            [_row("runner-a", runner.url)],
            {"runner-a": _snap(status=_status(), status_age_s=12)},
        )

    assert machine.online is True and machine.paused is False
    assert machine.running_sha == "def456" and machine.shell_count == 3
    assert machine.observed_at is not None
    assert 10 <= (datetime.now(UTC) - machine.observed_at).total_seconds() <= 20


def test_one_dropped_probe_keeps_the_last_status_and_its_age(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with runner_ops(
        db_conn, "runner-a", {"status_probe": completed(_status("runner-a"))}
    ) as runner:
        [machine] = _gather(
            database,
            machine_identity,
            tmp_path,
            [_row("runner-a", runner.url)],
            {"runner-a": _snap(reachable=False, failures=1, status=_status(), status_age_s=70)},
        )

    assert machine.online is True and machine.running_sha == "def456"
    assert machine.observed_at is not None
    assert (datetime.now(UTC) - machine.observed_at).total_seconds() >= 65  # the older answer


def test_a_machine_that_failed_its_passes_renders_offline(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with runner_ops(
        db_conn, "runner-a", {"status_probe": completed(_status("runner-a"))}
    ) as runner:
        [machine] = _gather(
            database,
            machine_identity,
            tmp_path,
            [_row("runner-a", runner.url)],
            {"runner-a": _snap(reachable=False, failures=2, status=_status())},
        )

    assert machine.online is False and machine.paused is None


def test_a_reachable_answer_that_is_not_a_cluster_status_is_online_unknown(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with runner_ops(
        db_conn, "runner-a", {"status_probe": completed(_status("runner-a"))}
    ) as runner:
        [machine] = _gather(
            database,
            machine_identity,
            tmp_path,
            [_row("runner-a", runner.url)],
            {
                "runner-a": _snap(reachable=True, status=None),
            },
        )

    assert machine.online is True and machine.paused is None


def test_a_snapshot_answered_by_another_host_renders_identity_mismatch(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with runner_ops(
        db_conn, "runner-a", {"status_probe": completed(_status("runner-a"))}
    ) as runner:
        [machine] = _gather(
            database,
            machine_identity,
            tmp_path,
            [_row("runner-a", runner.url)],
            {"runner-a": _snap(status=_status("somewhere-else"))},
        )

    assert machine.identity_mismatch is True and machine.online is False


def test_a_snapshot_body_that_is_not_a_cluster_status_renders_online_unknown(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    with runner_ops(
        db_conn, "runner-a", {"status_probe": completed(_status("runner-a"))}
    ) as runner:
        [machine] = _gather(
            database,
            machine_identity,
            tmp_path,
            [_row("runner-a", runner.url)],
            {"runner-a": _snap(status={"machine_name": "runner-a", "paused": True})},
        )

    assert machine.online is True and machine.paused is None


# --- when the gateway dials ---------------------------------------------------


def test_a_machine_without_a_fresh_snapshot_is_dialed(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    """The degraded fallback for a heartbeat service that is not running."""

    def reply(name: str) -> dict[str, Responder]:
        return {"status_probe": completed(_status(name))}

    with (
        runner_ops(db_conn, "fresh-one", reply("fresh-one")) as fresh_one,
        runner_ops(db_conn, "stale-one", reply("stale-one")) as stale_one,
        runner_ops(db_conn, "unknown-one", reply("unknown-one")) as unknown_one,
    ):
        machines = _gather(
            database,
            machine_identity,
            tmp_path,
            [
                _row("fresh-one", fresh_one.url),
                _row("stale-one", stale_one.url),
                _row("unknown-one", unknown_one.url),
            ],
            {
                "fresh-one": _snap(status=_status("fresh-one")),
                "stale-one": _snap(status=_status("stale-one"), age_s=3600),
            },
        )

    assert _dialed(fresh_one, stale_one, unknown_one) == ["stale-one", "unknown-one"]
    assert {m.name for m in machines} == {"fresh-one", "stale-one", "unknown-one"}


def test_a_fresh_read_dials_every_runner_whatever_the_snapshot_says(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    def reply(name: str) -> dict[str, Responder]:
        return {"status_probe": completed(_status(name))}

    with runner_ops(db_conn, "a", reply("a")) as a, runner_ops(db_conn, "b", reply("b")) as b:
        machines = _gather(
            database,
            machine_identity,
            tmp_path,
            [_row("a", a.url), _row("b", b.url)],
            None,
        )

    assert _dialed(a, b) == ["a", "b"]
    assert all(m.online and m.observed_at is None for m in machines)  # live, no age


def test_the_gateway_remembers_no_failure_between_reads(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    """A host that fails a dial is dialed again by the next fresh read: there is no
    failure memory and no recovery thread in the gateway."""
    with runner_ops(db_conn, "runner-a", {"status_probe": refused(404, "blackhole")}) as runner:
        rows = [_row("runner-a", runner.url)]
        first = _gather(database, machine_identity, tmp_path, rows, None)
        second = _gather(database, machine_identity, tmp_path, rows, None)

    assert [m.online for m in first + second] == [False, False]
    assert len(runner.calls_of("status_probe")) == 2


def test_a_fresh_read_of_blackhole_hosts_is_bounded_by_one_dial_budget(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    """The dials run in parallel under one per-dial deadline, so five hosts that
    never answer cost about one budget, not five."""
    budget_s = 0.3
    release = threading.Event()

    def never_answers(call: OpsCall) -> OpsReply:
        del call
        release.wait(timeout=5.0)
        return completed({})

    with runner_ops(db_conn, "blackhole", {"status_probe": never_answers}) as blackhole:
        started = time.monotonic()
        try:
            machines = _gather(
                database,
                machine_identity,
                tmp_path,
                [_row(f"h{i}", blackhole.url) for i in range(5)],
                None,
                probe_timeout_s=budget_s,
            )
        finally:
            release.set()
        elapsed = time.monotonic() - started

    assert [m.online for m in machines] == [False] * 5
    assert elapsed < budget_s * 3


# --- the endpoints ------------------------------------------------------------


def _snapshot_row(conn: psycopg.Connection, name: str, *, running_sha: str) -> None:
    conn.execute(
        "INSERT INTO machine_status_snapshot "
        "(machine_name, observed_at, reachable, consecutive_failures, status, status_at) "
        "VALUES (%s, now(), true, 0, %s, now())",
        (name, Jsonb(_status(name, running_sha=running_sha))),
    )
    conn.commit()


def test_the_roster_endpoint_reads_the_snapshot_and_fresh_dials(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    db_conn.execute("TRUNCATE machines")
    db_conn.commit()
    answer = {"status_probe": completed(_status("runner-snap", running_sha="dial"))}

    with (
        runner_ops(db_conn, "runner-snap", answer) as runner,
        gateway_test_client(machine=machine_identity) as client,
    ):
        _snapshot_row(db_conn, "runner-snap", running_sha="from-the-snapshot")
        default = client.get("/api/cluster/roster").json()
        fresh = client.get("/api/cluster/roster?fresh=true").json()

    row = next(m for m in default if m["name"] == "runner-snap")
    assert row["running_sha"] == "from-the-snapshot" and row["observed_at"] is not None
    live = next(m for m in fresh if m["name"] == "runner-snap")
    assert live["running_sha"] == "dial" and live["observed_at"] is None
    assert len(runner.calls_of("status_probe")) == 1


def test_the_machines_endpoint_follows_the_same_two_modes(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    db_conn.execute("TRUNCATE machines")
    db_conn.commit()
    answer = {"status_probe": completed(_status("runner-snap"))}

    with (
        runner_ops(db_conn, "runner-snap", answer) as runner,
        gateway_test_client(machine=machine_identity) as client,
    ):
        _snapshot_row(db_conn, "runner-snap", running_sha="x")
        assert client.get("/api/cluster/machines").json()[0]["live"] is True
        assert runner.calls == []
        assert client.get("/api/cluster/machines?fresh=true").json()[0]["live"] is True

    assert len(runner.calls_of("status_probe")) == 1
