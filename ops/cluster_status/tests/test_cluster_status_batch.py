"""Regression coverage for batched reads in the host status snapshot."""

from __future__ import annotations

import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import psycopg
import pytest

from base.api_contracts.status import SchemaMismatchStatus
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.schema.migrations import applied_migration_names
from base.host.resource_sample import ResourceSample
from base.native_process.loaded_commit import LoadedCommit
from ops import cluster_status
from ops.cluster_status import StatusSnapshotHooks, schema_mismatch
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


@dataclass
class _SnapshotDeps:
    agent_connections: list[object]
    hooks: StatusSnapshotHooks


# meiva:note
# Keep the snapshot focused on DB bundling and resource sampling. Yields the
# connections the agent count was read on, one per read, and the baseline hooks.
@pytest.fixture
def snapshot_dependencies(
    machine_identity: MachineIdentity,
) -> _SnapshotDeps:
    agent_connections: list[object] = []

    def _dead_pidfile(_path: str) -> tuple[bool, int | None]:
        return False, None

    def _no_sessions() -> tuple[list[SessionInfo], int, int]:
        return [], 0, 0

    def _no_agents(conn: object, *, local_machine: str) -> int:
        assert local_machine == machine_identity.name()
        agent_connections.append(conn)
        return 0

    def _schema_status(
        db: Database, *, conn: psycopg.Connection | None = None, local_machine: str
    ) -> SchemaMismatchStatus | None:
        return schema_mismatch.status(
            db,
            conn=conn,
            local_machine=local_machine,
            applied_reader=lambda _conn: {"baseline"},
            required_reader=lambda: {"baseline"},
        )

    machine_identity.set(name="runner-a", role=frozenset({"agent-runner"}))
    hooks = StatusSnapshotHooks(
        pidfile_checker=_dead_pidfile,
        session_collector=_no_sessions,
        agent_counter=_no_agents,
        schema_status_fn=_schema_status,
        head_sha_reader=lambda: None,
    )
    return _SnapshotDeps(agent_connections=agent_connections, hooks=hooks)


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


def test_collect_sessions_batches_timestamp_reads() -> None:
    """Each backend receives one timestamp batch, never one read per session."""
    service = _BatchOnlyBackend("meiva-main-agent-host")
    shell = _BatchOnlyBackend("meiva-main-agent-7-shell-0")

    sessions, _, _ = cluster_status.collect_sessions(
        service_backend=service,
        shell_backend=shell,
    )

    assert [session.name for session in sessions] == sorted([service.name, shell.name])
    assert service.batch_calls == [[service.name]]
    assert shell.batch_calls == [[shell.name]]


def test_collect_sessions_stamps_cluster_zone() -> None:
    """Session created_at renders in the cluster timezone (user ruling
    2026-08-27), never the host OS zone — a runner whose OS zone differs must
    show the same wall clock as the gateway."""

    import datetime as dt
    from zoneinfo import ZoneInfo

    service = _BatchOnlyBackend("meiva-main-agent-host")
    sessions, _, _ = cluster_status.collect_sessions(
        service_backend=service,
        shell_backend=_BatchOnlyBackend("x"),
        clock=Clock(
            ClockConfig(
                timezone="Asia/Shanghai",
                authoritative_timezone="Asia/Shanghai",
                message_timestamp_weekday=False,
            ),
            timers=SYSTEM_TIMERS,
        ),
    )
    created = sessions[0].created_at
    assert created is not None
    # epoch 1000 = 1970-01-01 00:16:40 UTC = 1970-01-01 08:16:40 +08:00
    assert created == dt.datetime(1970, 1, 1, 8, 16, 40, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_status_snapshot_uses_one_connection_while_sampling_resources(
    snapshot_dependencies: _SnapshotDeps,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """One snapshot shares one connection while its one live sample runs in parallel."""
    agent_connections = snapshot_dependencies.agent_connections
    conn = object()
    connect_calls = 0
    schema_connections: list[object] = []
    sample_started = threading.Event()
    db_finished = threading.Event()
    sample_calls = 0

    class _OverlapDb:
        @contextmanager
        def connect(self, *, autocommit: bool = False) -> Generator[object, None, None]:
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

    def _schema_status(
        db: Database, *, conn: psycopg.Connection | None = None, local_machine: str
    ) -> SchemaMismatchStatus | None:
        return schema_mismatch.status(
            db,
            conn=conn,
            local_machine=local_machine,
            applied_reader=lambda schema_conn: (
                schema_connections.append(schema_conn) or {"baseline"}
            ),
            required_reader=lambda: {"baseline"},
        )

    snapshot = cluster_status.status_snapshot(
        cast(Database, _OverlapDb()),
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
        hooks=replace(
            snapshot_dependencies.hooks,
            schema_status_fn=_schema_status,
            resource_sampler=_sample,
        ),
    )

    assert connect_calls == 1
    assert agent_connections == [conn]
    assert schema_connections == [conn]
    assert sample_calls == 1
    assert snapshot.resource == _RESOURCE


def test_status_snapshot_borrows_pool_once_with_a_bounded_timeout(
    snapshot_dependencies: _SnapshotDeps,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """The ops daemon's pool contributes one bounded borrow, not fresh dials."""
    agent_connections = snapshot_dependencies.agent_connections
    conn = object()
    pool = _Pool(conn)

    class _ForbidDirectConnectDb:
        def connect(self, **_kwargs: object) -> object:
            raise AssertionError("pool-backed snapshot opened a fresh DB connection")

    cluster_status.status_snapshot(
        cast(Database, _ForbidDirectConnectDb()),
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
        hooks=replace(snapshot_dependencies.hooks, resource_sampler=lambda: _RESOURCE),
    )

    assert pool.timeouts == [2.0]
    assert agent_connections == [conn]


def test_two_status_snapshots_do_not_cache_db_or_resource_reads(
    snapshot_dependencies: _SnapshotDeps,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Sharing is snapshot-local: every later probe reads DB and resources again."""
    agent_connections = snapshot_dependencies.agent_connections
    pool = _Pool(object())
    sample_reads = 0

    def _sample() -> ResourceSample:
        nonlocal sample_reads
        sample_reads += 1
        return _RESOURCE

    hooks = replace(snapshot_dependencies.hooks, resource_sampler=_sample)

    cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
        hooks=hooks,
    )
    cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
        hooks=hooks,
    )

    assert pool.timeouts == [2.0, 2.0]
    assert agent_connections == [pool.conn, pool.conn]
    assert sample_reads == 2


# meiva:note
# DB-down is a valid snapshot: it cannot claim readiness, and says why.
def test_status_snapshot_degrades_when_the_pool_cannot_reach_db(
    snapshot_dependencies: _SnapshotDeps,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    pool = _Pool(object(), error=RuntimeError("DB down"))

    snapshot = cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
        hooks=replace(snapshot_dependencies.hooks, resource_sampler=lambda: _RESOURCE),
    )

    assert pool.timeouts == [2.0]
    assert snapshot.paused is True  # A missing DB snapshot cannot claim readiness.
    assert snapshot.paused_reason == "no_state"
    assert snapshot.agent_count == 0
    assert snapshot.resource == _RESOURCE
    assert snapshot.schema_mismatch is not None
    assert snapshot.schema_mismatch.kind == "unavailable"
    assert "status database snapshot failed" in snapshot.schema_mismatch.detail


def test_status_snapshot_preserves_invalid_real_catalog_diagnosis(
    db_conn: psycopg.Connection,
    snapshot_dependencies: _SnapshotDeps,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    pool = _Pool(db_conn)

    def _real_catalog_schema_status(
        db: Database, *, conn: psycopg.Connection | None = None, local_machine: str
    ) -> SchemaMismatchStatus | None:
        return schema_mismatch.status(
            db,
            conn=conn,
            local_machine=local_machine,
            applied_reader=applied_migration_names,
            required_reader=lambda: {"baseline"},
        )

    with db_conn.transaction(force_rollback=True):
        db_conn.execute("ALTER TABLE schema_migrations RENAME COLUMN name TO unexpected_name")
        snapshot = cluster_status.status_snapshot(
            database,
            pool=pool,
            image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
            local_machine=machine_identity,
            hooks=replace(
                snapshot_dependencies.hooks,
                schema_status_fn=_real_catalog_schema_status,
                resource_sampler=lambda: _RESOURCE,
            ),
        )
        assert snapshot.schema_mismatch is not None
        assert snapshot.schema_mismatch.kind == "invalid-migration-layout"
        assert "unrecognized shape" in snapshot.schema_mismatch.detail
        assert snapshot.resource == _RESOURCE
        assert pool.timeouts == [2.0]


def test_resource_sample_failure_still_degrades_to_none_from_worker(
    snapshot_dependencies: _SnapshotDeps,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """Moving the sample to a worker must not let its exception fail the probe."""
    pool = _Pool(object())

    def _sample_failure() -> ResourceSample:
        raise RuntimeError("psutil unavailable")

    snapshot = cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
        hooks=replace(snapshot_dependencies.hooks, resource_sampler=_sample_failure),
    )

    assert snapshot.resource is None


def test_agent_count_reads_local_retained_identities_without_processes(
    db_conn: psycopg.Connection,
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


def test_agent_count_uses_the_same_borrow_and_reaches_the_snapshot(
    snapshot_dependencies: _SnapshotDeps,
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

    snapshot = cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
        hooks=replace(
            snapshot_dependencies.hooks,
            agent_counter=count,
            resource_reader=lambda: None,
        ),
    )
    assert snapshot.agent_count == 7
    assert seen == [conn]
    assert pool.timeouts == [2.0]


@pytest.mark.parametrize("runner", [False, True])
def test_agent_host_liveness_is_probed_only_on_a_runner(
    snapshot_dependencies: _SnapshotDeps,
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

    snapshot = cluster_status.status_snapshot(
        database,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
        hooks=replace(
            snapshot_dependencies.hooks,
            pidfile_checker=check,
            resource_reader=lambda: None,
        ),
    )
    assert snapshot.agent_host_online is (True if runner else None)
    assert (str(ServiceEndpoints.from_settings().of("agent_host").pidfile) in probes) is runner
    assert "restarter_online" not in snapshot.model_dump()


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
    snapshot_dependencies: _SnapshotDeps,
    db_ok: bool,
    serving: bool,
    expected_reason: str | None,
    database: Database,
    *,
    machine_identity: MachineIdentity,
) -> None:
    """`paused` stays an additive-compatible bool; `paused_reason` tells the causes apart."""
    pool = _Pool(object(), error=None if db_ok else RuntimeError("DB down"))

    snapshot = cluster_status.status_snapshot(
        database,
        pool=pool,
        image=LoadedCommit(source_root=Path(__file__).parent, sha=None),
        local_machine=machine_identity,
        hooks=replace(
            snapshot_dependencies.hooks,
            is_serving_fn=lambda: serving,
            resource_sampler=lambda: _RESOURCE,
        ),
    )

    assert snapshot.paused is (expected_reason is not None)
    assert snapshot.paused_reason == expected_reason
