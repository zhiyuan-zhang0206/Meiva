"""Regression coverage for batched reads in the host status snapshot."""

from __future__ import annotations

import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest

from base import db
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.git import cluster_drift
from base.deploy.lifecycle import start_serving
from base.host import resource_sample
from base.host.resource_sample import ResourceSample
from base.native_process.loaded_commit import LoadedCommit
from base.sessions import backend
from ops import cluster_status
from ops.cluster_status import schema_mismatch
from ops.rpc_schemas import SessionInfo

_RESOURCE = ResourceSample(
    ts=1.0,
    cpu_pct=2.0,
    mem_used_gb=3.0,
    mem_total_gb=4.0,
    mem_pct=5.0,
    disk_used_gb=6.0,
    disk_total_gb=7.0,
    disk_pct=8.0,
)


def testcheck_pidfile_ignores_root_level_pidfile(tmp_path: Path) -> None:
    current = tmp_path / "run" / "agent_host.pid"
    current.parent.mkdir()
    (tmp_path / current.name).write_text(str(os.getpid()))

    assert cluster_status.check_pidfile(str(current)) == (False, None)

    current.write_text(str(os.getpid()))
    assert cluster_status.check_pidfile(str(current)) == (True, os.getpid())


class _Pool:
    def __init__(self, conn: object, *, error: Exception | None = None) -> None:
        self.conn = conn
        self.error = error
        self.timeouts: list[float] = []

    @contextmanager
    def connection(self, *, timeout: float) -> Generator[object, None, None]:
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        yield self.conn


# meiva:note
# Keep the snapshot focused on DB bundling and resource sampling. Yields the
# connections the agent count was read on, one per read.
@pytest.fixture
def snapshot_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentity,
) -> list[object]:
    agent_connections: list[object] = []

    def _dead_pidfile(_path: str) -> tuple[bool, int | None]:
        return False, None

    def _no_sessions() -> tuple[list[SessionInfo], int, int]:
        return [], 0, 0

    monkeypatch.setattr(cluster_status, "check_pidfile", _dead_pidfile)
    monkeypatch.setattr(cluster_status, "collect_sessions", _no_sessions)

    def _no_agents(conn: object, *, local_machine: str) -> int:
        assert local_machine == machine_identity.name()
        agent_connections.append(conn)
        return 0

    def _applied(_conn: object) -> set[str]:
        return {"baseline"}

    monkeypatch.setattr(cluster_status, "count_local_agents", _no_agents)
    monkeypatch.setattr(schema_mismatch, "applied_migration_names", _applied)
    monkeypatch.setattr(schema_mismatch, "required_migration_set", lambda: {"baseline"})
    machine_identity.set(name="runner-a", role=frozenset({"agent-runner"}))
    monkeypatch.setattr(cluster_drift, "prod_source_head_sha", lambda: None)
    return agent_connections


class _BatchOnlyBackend:
    def __init__(self, name: str):
        self.name = name
        self.batch_calls: list[list[str]] = []

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [self.name] if self.name.startswith(prefix) else []

    def session_started_at(self, name: str) -> float | None:
        raise AssertionError(f"single timestamp read used for {name}")

    def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
        self.batch_calls.append(names)
        return dict.fromkeys(names, 1000.0)


def test_collect_sessions_batches_timestamp_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each backend receives one timestamp batch, never one read per session."""
    service = _BatchOnlyBackend("meiva-main-agent-host")
    shell = _BatchOnlyBackend("meiva-main-agent-7-shell-0")
    monkeypatch.setattr(backend, "get_backend", lambda: service)
    monkeypatch.setattr(backend, "get_shell_backend", lambda: shell)

    sessions, _, _ = cluster_status.collect_sessions()

    assert [session.name for session in sessions] == sorted([service.name, shell.name])
    assert service.batch_calls == [[service.name]]
    assert shell.batch_calls == [[shell.name]]


def test_collect_sessions_stamps_cluster_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session created_at renders in the cluster timezone (user ruling
    2026-08-27), never the host OS zone — a runner whose OS zone differs must
    show the same wall clock as the gateway."""

    import datetime as dt
    from zoneinfo import ZoneInfo

    service = _BatchOnlyBackend("meiva-main-agent-host")
    monkeypatch.setattr(backend, "get_backend", lambda: service)
    monkeypatch.setattr(backend, "get_shell_backend", lambda: _BatchOnlyBackend("x"))
    from base.config import settings
    from base.config.domains.general import GeneralSettings

    monkeypatch.setattr(
        settings, "general", GeneralSettings.model_construct(timezone="Asia/Shanghai")
    )

    sessions, _, _ = cluster_status.collect_sessions()
    created = sessions[0].created_at
    assert created is not None
    # epoch 1000 = 1970-01-01 00:16:40 UTC = 1970-01-01 08:16:40 +08:00
    assert created == dt.datetime(1970, 1, 1, 8, 16, 40, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_status_snapshot_uses_one_connection_while_sampling_resources(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: list[object],
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """One snapshot shares one connection while its one live sample runs in parallel."""
    agent_connections = snapshot_dependencies
    conn = object()
    connect_calls = 0
    schema_connections: list[object] = []
    sample_started = threading.Event()
    db_finished = threading.Event()
    sample_calls = 0

    @contextmanager
    def _connect(_self: Database, *, autocommit: bool = False) -> Generator[object, None, None]:
        nonlocal connect_calls
        connect_calls += 1
        assert autocommit is True
        assert sample_started.wait(timeout=2), "resource sampling did not overlap the DB read"
        try:
            yield conn
        finally:
            db_finished.set()

    def _sample() -> ResourceSample:
        nonlocal sample_calls
        sample_calls += 1
        sample_started.set()
        assert db_finished.wait(timeout=2), "DB reads did not overlap the resource sample"
        return _RESOURCE

    def _applied(schema_conn: object) -> set[str]:
        schema_connections.append(schema_conn)
        return {"baseline"}

    monkeypatch.setattr(schema_mismatch, "applied_migration_names", _applied)
    monkeypatch.setattr(schema_mismatch, "required_migration_set", lambda: {"baseline"})

    monkeypatch.setattr(Database, "connect", _connect)
    monkeypatch.setattr(resource_sample, "resource_sample", _sample)

    snapshot = cluster_status.status_snapshot(
        database,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
    )

    assert connect_calls == 1
    assert agent_connections == [conn]
    assert schema_connections == [conn]
    assert sample_calls == 1
    assert snapshot.resource == _RESOURCE


def test_status_snapshot_borrows_pool_once_with_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: list[object],
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The ops daemon's pool contributes one bounded borrow, not fresh dials."""
    agent_connections = snapshot_dependencies
    conn = object()
    pool = _Pool(conn)

    def _fresh_connect(**_kwargs: object) -> object:
        raise AssertionError("pool-backed snapshot opened a fresh DB connection")

    monkeypatch.setattr(db, "connect", _fresh_connect)
    monkeypatch.setattr(resource_sample, "resource_sample", lambda: _RESOURCE)

    cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
    )

    assert pool.timeouts == [2.0]
    assert agent_connections == [conn]


def test_two_status_snapshots_do_not_cache_db_or_resource_reads(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_dependencies: list[object],
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Sharing is snapshot-local: every later probe reads DB and resources again."""
    agent_connections = snapshot_dependencies
    pool = _Pool(object())
    sample_reads = 0

    def _sample() -> ResourceSample:
        nonlocal sample_reads
        sample_reads += 1
        return _RESOURCE

    monkeypatch.setattr(resource_sample, "resource_sample", _sample)

    cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
    )
    cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
    )

    assert pool.timeouts == [2.0, 2.0]
    assert agent_connections == [pool.conn, pool.conn]
    assert sample_reads == 2


# meiva:note
# DB-down is a valid snapshot: it cannot claim readiness, and says why.
@pytest.mark.usefixtures("snapshot_dependencies")
def test_status_snapshot_degrades_when_the_pool_cannot_reach_db(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    pool = _Pool(object(), error=RuntimeError("DB down"))
    monkeypatch.setattr(resource_sample, "resource_sample", lambda: _RESOURCE)

    snapshot = cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
    )

    assert pool.timeouts == [2.0]
    assert snapshot.paused is True  # A missing DB snapshot cannot claim readiness.
    assert snapshot.paused_reason == "no_state"
    assert snapshot.agent_count == 0
    assert snapshot.resource == _RESOURCE
    assert snapshot.schema_mismatch is not None
    assert snapshot.schema_mismatch.kind == "unavailable"
    assert "status database snapshot failed" in snapshot.schema_mismatch.detail


@pytest.mark.usefixtures("snapshot_dependencies")
def test_status_snapshot_preserves_invalid_real_catalog_diagnosis(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from base.deploy.schema.migrations import applied_migration_names

    pool = _Pool(db_conn)

    monkeypatch.setattr(resource_sample, "resource_sample", lambda: _RESOURCE)
    monkeypatch.setattr(schema_mismatch, "applied_migration_names", applied_migration_names)
    with db_conn.transaction(force_rollback=True):
        db_conn.execute("ALTER TABLE schema_migrations RENAME COLUMN name TO unexpected_name")
        snapshot = cluster_status.status_snapshot(
            database,
            pool=pool,
            image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
            local_machine=machine_identity,
        )
        assert snapshot.schema_mismatch is not None
        assert snapshot.schema_mismatch.kind == "invalid-migration-layout"
        assert "unrecognized shape" in snapshot.schema_mismatch.detail
        assert snapshot.resource == _RESOURCE
        assert pool.timeouts == [2.0]


@pytest.mark.usefixtures("snapshot_dependencies")
def test_resource_sample_failure_still_degrades_to_none_from_worker(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Moving the sample to a worker must not let its exception fail the probe."""
    pool = _Pool(object())

    def _sample_failure() -> ResourceSample:
        raise RuntimeError("psutil unavailable")

    monkeypatch.setattr(resource_sample, "resource_sample", _sample_failure)

    snapshot = cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
    )

    assert snapshot.resource is None


def test_agent_count_reads_local_retained_identities_without_processes(
    db_conn: psycopg.Connection,
    monkeypatch: pytest.MonkeyPatch,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from base.db import create_agent

    machine_identity.set(name="count-host")
    for machine, status in (
        ("count-host", "running"),
        ("count-host", "idling"),
        ("count-host", "idling"),
        ("count-host", "terminated"),
        ("other-host", "idling"),
    ):
        agent = create_agent(db_conn)
        db_conn.execute(
            "INSERT INTO agents_meta(id,machine,status) VALUES(%s,%s,%s)",
            (agent, machine, status),
        )
    db_conn.commit()
    assert cluster_status.count_local_agents(db_conn, local_machine=machine_identity.name()) == 3


@pytest.mark.usefixtures("snapshot_dependencies")
def test_agent_count_uses_the_same_borrow_and_reaches_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    conn = object()
    pool = _Pool(conn)
    seen: list[object] = []

    def count(connection: object, *, local_machine: str) -> int:
        assert local_machine == machine_identity.name()
        seen.append(connection)
        return 7

    monkeypatch.setattr(cluster_status, "count_local_agents", count)
    monkeypatch.setattr(cluster_status, "read_resource_sample", lambda: None)
    snapshot = cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
    )
    assert snapshot.agent_count == 7
    assert seen == [conn]
    assert pool.timeouts == [2.0]


@pytest.mark.parametrize("runner", [False, True])
@pytest.mark.usefixtures("snapshot_dependencies")
def test_agent_host_liveness_is_probed_only_on_a_runner(
    monkeypatch: pytest.MonkeyPatch,
    runner: bool,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    from base.daemon.endpoints import ServiceEndpoints

    probes: list[str] = []

    def check(path: str) -> tuple[bool, int]:
        probes.append(path)
        return True, 1234

    machine_identity.set(role=frozenset({"agent-runner" if runner else "gateway"}))
    monkeypatch.setattr(cluster_status, "check_pidfile", check)

    monkeypatch.setattr(cluster_status, "read_resource_sample", lambda: None)
    snapshot = cluster_status.status_snapshot(
        database,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
    )
    assert snapshot.agent_host_online is (True if runner else None)
    assert (str(ServiceEndpoints.from_settings().of("agent_host").pidfile) in probes) is runner
    assert "restarter_online" not in snapshot.model_dump()


@pytest.mark.usefixtures("snapshot_dependencies")
@pytest.mark.parametrize(
    ("db_ok", "serving", "expected_reason"),
    [
        # meiva:note
        # A start in flight, a stop that has begun, or a failed start that parked the
        # serving gate short of `serving` while the database reads fine: the reason
        # names the gate, so it never reads like a failed database snapshot (task #3404).
        (True, False, "startup"),
        # meiva:note
        # An unreadable database snapshot is the verdict's first clause: it outranks
        # every other cause, including a parked gate.
        (False, True, "no_state"),
        (False, False, "no_state"),
        # No clause fired.
        (True, True, None),
    ],
)
def test_status_snapshot_paused_reason_names_the_first_true_clause(
    monkeypatch: pytest.MonkeyPatch,
    db_ok: bool,
    serving: bool,
    expected_reason: str | None,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """`paused` stays an additive-compatible bool; `paused_reason` tells the causes apart."""
    pool = _Pool(object(), error=None if db_ok else RuntimeError("DB down"))
    monkeypatch.setattr(start_serving, "is_serving", lambda: serving)
    monkeypatch.setattr(resource_sample, "resource_sample", lambda: _RESOURCE)

    snapshot = cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
    )

    assert snapshot.paused is (expected_reason is not None)
    assert snapshot.paused_reason == expected_reason
