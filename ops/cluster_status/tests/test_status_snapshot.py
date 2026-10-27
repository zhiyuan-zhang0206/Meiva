# meiva:note
# A host's status snapshot read from its real resources.
#
# The pidfile is a file under the test's home holding a live or reaped pid, the agent identities
# are rows of the throwaway Postgres, the pool is a real `ConnectionPool` (closed, or exhausted
# by a held connection, for the degraded reads), the sessions are real shells served by the
# `pty_service` fixture's service, and the resource sample is the host's own psutil reading. The
# serving gate, the checkout HEAD and the root supervisor are read for real in
# `gateway/tests/routers/cluster_endpoints/test_status_snapshot.py`. What the facts mean is
# decided in `report` and asserted on values in `test_report.py`.

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database, create_agent
from base.native_process.loaded_commit import LoadedCommit
from base.sessions.pty.tests.pty_shells import new
from ops import cluster_status
from ops.cluster_status import (
    check_pidfile,
    count_local_agents,
    read_db_snapshot,
    read_resource_sample,
)
from ops.rpc_schemas import AgentSessionGroup

_RUNNER = frozenset({"agent-runner"})


def _image(tmp_path: Path) -> LoadedCommit:
    return LoadedCommit(source_root=tmp_path, sha="abc1234")


def _insert_agents(conn: psycopg.Connection, *rows: tuple[str, str]) -> None:
    for machine, status in rows:
        agent = create_agent(conn)
        conn.execute(
            "INSERT INTO agents_meta(id,machine,status) VALUES(%s,%s,%s)", (agent, machine, status)
        )
    conn.commit()


@pytest.fixture
def pool(db_url: str) -> Iterator[ConnectionPool]:
    pool: ConnectionPool = ConnectionPool(
        db_url, min_size=1, max_size=1, kwargs={"prepare_threshold": None}
    )
    with pool:
        yield pool


def test_a_pidfile_of_a_live_process_reads_alive(tmp_path: Path) -> None:
    pidfile = tmp_path / "live.pid"
    pidfile.write_text(str(os.getpid()))

    assert check_pidfile(str(pidfile)) == (True, os.getpid())


def test_a_pidfile_of_a_process_that_exited_reads_dead_with_its_pid(tmp_path: Path) -> None:
    child = subprocess.Popen([sys.executable, "--version"], stdout=subprocess.DEVNULL)
    child.wait()
    pidfile = tmp_path / "dead.pid"
    pidfile.write_text(str(child.pid))

    assert check_pidfile(str(pidfile)) == (False, child.pid)


@pytest.mark.parametrize("content", [None, "", "not-a-pid\n"])
def test_a_missing_empty_or_garbled_pidfile_has_no_pid(tmp_path: Path, content: str | None) -> None:
    pidfile = tmp_path / "x.pid"
    if content is not None:
        pidfile.write_text(content)

    assert check_pidfile(str(pidfile)) == (False, None)


def test_a_pidfile_one_level_up_is_not_the_daemons(tmp_path: Path) -> None:
    current = tmp_path / "run" / "agent_host.pid"
    current.parent.mkdir()
    (tmp_path / current.name).write_text(str(os.getpid()))

    assert check_pidfile(str(current)) == (False, None)

    current.write_text(str(os.getpid()))
    assert check_pidfile(str(current)) == (True, os.getpid())


def test_the_agent_count_is_this_machines_retained_identities(db_conn: psycopg.Connection) -> None:
    _insert_agents(
        db_conn,
        ("count-host", "running"),
        ("count-host", "idling"),
        ("count-host", "idling"),
        ("count-host", "terminated"),
        ("other-host", "idling"),
    )

    assert count_local_agents(db_conn, local_machine="count-host") == 3


def test_a_snapshot_read_borrows_one_connection_for_the_count_and_the_schema(
    pool: ConnectionPool, database: Database, db_conn: psycopg.Connection
) -> None:
    _insert_agents(db_conn, ("runner-a", "running"), ("runner-a", "idling"), ("other", "idling"))

    snapshot = read_db_snapshot(database, pool, machine_name="runner-a", count_agents=True)

    assert (snapshot.ok, snapshot.agent_count, snapshot.schema_mismatch) == (True, 2, None)


# meiva:note
# Sharing is snapshot-local: every later probe reads the database again.
def test_two_snapshots_do_not_cache_the_database_read(
    pool: ConnectionPool, database: Database, db_conn: psycopg.Connection
) -> None:
    _insert_agents(db_conn, ("runner-a", "running"))
    first = read_db_snapshot(database, pool, machine_name="runner-a", count_agents=True)

    _insert_agents(db_conn, ("runner-a", "idling"))
    second = read_db_snapshot(database, pool, machine_name="runner-a", count_agents=True)

    assert (first.agent_count, second.agent_count) == (1, 2)


def test_a_host_that_is_not_a_runner_counts_no_agents(
    database: Database, db_conn: psycopg.Connection
) -> None:
    _insert_agents(db_conn, ("gateway-a", "running"))

    snapshot = read_db_snapshot(database, None, machine_name="gateway-a", count_agents=False)

    assert (snapshot.ok, snapshot.agent_count) == (True, 0)


def test_a_pool_that_is_closed_degrades_the_snapshot(
    pool: ConnectionPool, database: Database
) -> None:
    pool.close()

    snapshot = read_db_snapshot(database, pool, machine_name="runner-a", count_agents=True)

    assert (snapshot.ok, snapshot.agent_count) == (False, 0)
    assert snapshot.schema_mismatch is not None
    assert snapshot.schema_mismatch.kind == "unavailable"
    assert snapshot.schema_mismatch.machine == "runner-a"
    assert "status database snapshot failed (PoolClosed)" in snapshot.schema_mismatch.detail


# meiva:note
# A pool whose only connection is held does not hand one out: the snapshot waits for its own
# short bound, not the pool's long default, and then degrades.
def test_a_pool_with_no_free_connection_degrades_within_the_probe_bound(
    db_url: str, database: Database
) -> None:
    held: ConnectionPool = ConnectionPool(
        db_url, min_size=1, max_size=1, timeout=60.0, kwargs={"prepare_threshold": None}
    )
    with held, held.connection():
        started = time.monotonic()
        snapshot = read_db_snapshot(database, held, machine_name="runner-a", count_agents=True)
        waited = time.monotonic() - started

    assert snapshot.ok is False
    assert snapshot.schema_mismatch is not None
    assert "PoolTimeout" in snapshot.schema_mismatch.detail
    assert waited < 30.0


def test_a_runner_reports_its_agents_shells_daemons_and_resources(
    unit_home: Path,
    tmp_path: Path,
    pool: ConnectionPool,
    database: Database,
    db_conn: psycopg.Connection,
    pty_service: object,
    *,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(name="runner-a", role=_RUNNER)
    _insert_agents(db_conn, ("runner-a", "running"), ("runner-a", "idling"))
    for name in ("meiva-agent-7-shell-0", "meiva-agent-7-shell-1-watcher", "meiva-agent-9"):
        new(name, unit_home)
    pidfile = ServiceEndpoints.from_settings().of("agent_host").pidfile
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))

    status = cluster_status.status_snapshot(
        database, pool, image=_image(tmp_path), local_machine=machine_identity, home=unit_home
    )

    assert status.machine_name == "runner-a"
    assert (status.serve_gateway, status.serve_agent_runner) == (False, True)
    assert status.running_sha == "abc1234"
    assert status.agent_count == 2
    assert (status.shell_count, status.session_count) == (2, 2)
    groups = [AgentSessionGroup.model_validate(group) for group in status.agent_groups]
    assert [(g.agent_id, [s.name for s in g.shells]) for g in groups] == [
        (7, ["meiva-agent-7-shell-0", "meiva-agent-7-shell-1-watcher"])
    ]
    assert status.agent_host_online is True
    assert status.schema_mismatch is None
    assert status.resource is not None
    assert status.resource.mem_total_gb > 0
    # meiva:note
    # The home has not published a serving state, so the gate reads `startup` while the database
    # reads fine; the gateway's snapshot test publishes one and reads it unpaused.
    assert (status.paused, status.paused_reason) == (True, "startup")


def test_a_gateway_probes_no_agent_host_and_counts_no_agents(
    tmp_path: Path,
    database: Database,
    db_conn: psycopg.Connection,
    unit_home: Path,
    *,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(name="gateway-a", role=frozenset({"gateway"}))
    _insert_agents(db_conn, ("gateway-a", "running"))
    pidfile = ServiceEndpoints.from_settings().of("agent_host").pidfile
    pidfile.parent.mkdir(parents=True, exist_ok=True)
    pidfile.write_text(str(os.getpid()))

    status = cluster_status.status_snapshot(
        database, image=_image(tmp_path), local_machine=machine_identity, home=unit_home
    )

    assert status.serve_gateway is True
    assert status.agent_host_online is None
    assert status.agent_count == 0
    assert "restarter_online" not in status.model_dump()


def test_a_runner_whose_agent_host_left_no_live_pid_reads_offline(
    tmp_path: Path,
    database: Database,
    unit_home: Path,
    *,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(name="runner-a", role=_RUNNER)

    status = cluster_status.status_snapshot(
        database, image=_image(tmp_path), local_machine=machine_identity, home=unit_home
    )

    assert status.agent_host_online is False


def test_a_snapshot_with_an_unreachable_database_still_answers_but_claims_no_readiness(
    tmp_path: Path,
    pool: ConnectionPool,
    database: Database,
    unit_home: Path,
    *,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(name="runner-a", role=_RUNNER)
    pool.close()

    status = cluster_status.status_snapshot(
        database, pool, image=_image(tmp_path), local_machine=machine_identity, home=unit_home
    )

    assert (status.paused, status.paused_reason) == (True, "no_state")
    assert status.agent_count == 0
    assert status.resource is not None
    assert status.schema_mismatch is not None
    assert status.schema_mismatch.kind == "unavailable"


def test_the_resource_sample_is_the_hosts_own_reading() -> None:
    sample = read_resource_sample()

    assert sample is not None
    assert sample.mem_total_gb > 0
    assert sample.disk_total_gb > 0
    assert 0.0 <= sample.cpu_pct <= 100.0
