from __future__ import annotations

import asyncio
import os
import subprocess
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import psutil
import psycopg
import pytest
from psycopg.types.json import Jsonb

from base.clock.tests.fakes import system_clock
from base.cluster.machine import MachineIdentityReader
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.start_serving import RootBirth
from base.deploy.lifecycle.tests.serving_root import observe
from base.lm.catalog import ModelCatalog
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.ipc import RequestPayload, ResponsePayload, ok_response
from base.paths import root_run_dir
from base.paths.home import resolve_meiva_home
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.cluster.roster.roster_probe import IdentityMismatchLog
from gateway.cluster.status import RosterEnv
from gateway.tests.auth.gateway_config import gateway_test_rpc
from services.supervision.meiva_root.server import ControlServer


# meiva:note
# The gateway-process facts one roster read needs, with a fresh mismatch log.
def roster_env(
    database: Database,
    machine: MachineIdentityReader,
    *,
    home: Path,
    probe_timeout_s: float = 8.0,
) -> RosterEnv:
    return RosterEnv(
        db=database,
        identity_log=IdentityMismatchLog(),
        image=LOADED_IMAGE,
        home=home,
        machine=machine,
        probe_timeout_s=probe_timeout_s,
        clock=system_clock(),
        rpc=gateway_test_rpc(),
        # meiva:note
        # This helper renders rows, not the gate: the observation is the real one, as the
        # gateway process names it, and no case here marks a root.
        observe=start_serving.observe_root,
        environ=dict(os.environ),
    )


# meiva:note
# One `telemetry_events` row the admin events endpoint reads, committed.
def seed_event(
    conn: psycopg.Connection,
    *,
    event_name: str = "log",
    level: str = "info",
    agent_id: int | None = None,
    msg: str = "hi",
    seconds_ago: float = 60.0,
    category: str = "telemetry",
) -> None:
    conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
        "event_name, level, source, agent_id, attributes) "
        "VALUES (%s, now() - make_interval(secs => %s), 'machine-1', 'c', 'gateway', %s, %s, %s, "
        "'test', %s, %s)",
        (
            uuid.uuid4().int % (1 << 62),
            seconds_ago,
            category,
            event_name,
            level,
            agent_id,
            Jsonb({"msg": msg}),
        ),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def pinned_session_names() -> None:
    """Keep the shared fixture entry point for cluster endpoint test suites."""
    return


def seed_away_machine(
    db_conn: psycopg.Connection, *, name: str = "away", local_row: bool = True
) -> None:
    # meiva:note
    # Machines rows for the pause tests, isolated from other tests' rows.
    with db_conn.cursor() as cur:
        cur.execute("TRUNCATE machines")
        if local_row:
            cur.execute(
                "INSERT INTO machines (name, role, gateway_url) "
                "VALUES ('test-host', ARRAY['gateway'], NULL)"
            )
        cur.execute(
            "INSERT INTO machines (name, role, gateway_url) "
            "VALUES (%s, ARRAY['agent-runner'], NULL)",
            (name,),
        )
    db_conn.commit()


def seed_agent_on_machine(
    db_conn: psycopg.Connection,
    machine: str,
    *,
    status: str = "idling",
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> int:
    # meiva:note
    # One live agent row homed on `machine`.
    from base.agents.tests.units import spawn_agent

    aid = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE agents_meta SET machine = %s, status = %s WHERE id = %s",
            (machine, status, aid),
        )
    db_conn.commit()
    return aid


# meiva:note
# The pause endpoint reassigns a paused machine's tasks to a fixed drain owner
# (`_MACHINE_PAUSE_DRAIN_OWNER`, #405), so the row has to exist here as the FK
# target. Writing an id by hand does not move the id sequence, and the next spawn
# then takes a number that is already in the table: with two spawns, the second
# took 405 and the insert failed on `agents_pkey`. Moving the sequence past every
# id the table holds makes the manual row a reservation instead of a trap.
# meiva:link gateway/tests/routers/cluster_endpoints/test_admin_machine_delete.py
def seed_drain_owner(db_conn: psycopg.Connection, agent_id: int = 405) -> None:
    # meiva:note
    # The drain-owner agent row the pause endpoint reassigns tasks to.
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agents (id) VALUES (%s) ON CONFLICT (id) DO NOTHING",
            (agent_id,),
        )
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('agents', 'id'), (SELECT max(id) FROM agents))"
        )
    db_conn.commit()


def seed_in_progress_task(db_conn: psycopg.Connection, owner: int, title: str) -> int:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_tasks (title, description, status, owner, created_by) "
            "VALUES (%s, 'desc', 'in_progress', %s, 'user') RETURNING id",
            (title, owner),
        )
        row = cur.fetchone()
        assert row is not None
        (task_id,) = row
    db_conn.commit()
    return task_id


# meiva:note
# The serving gate, as the home's own state file: a completed start (`serving`) is the root
# publishing its birth evidence. Without this fixture the unit has not reached `serving`, so the
# local machine reads `paused` with reason `startup`.
@pytest.fixture
def serving_gate(serving_root: RootBirth) -> Generator[None]:
    home = resolve_meiva_home()
    generation = start_serving.begin_start(home)
    assert start_serving.mark_serving(
        home, generation, runtime=serving_root.runtime, observe=observe
    )
    try:
        yield
    finally:
        start_serving.clear_serving(home)


# meiva:note
# One git command in a temp checkout, with a fixed identity; returns its output.
def _git(repo: Path, *args: str) -> str:
    identity = ["-c", "user.name=test", "-c", "user.email=test@example.invalid"]
    return subprocess.run(  # noqa: S603
        ["git", *identity, *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


# meiva:note
# A real git checkout at `<home>/source` (where a unit's prod source lives) holding one commit;
# returns its HEAD sha.
def prod_source_checkout(home: Path) -> str:
    source = home / "source"
    source.mkdir()
    _git(source, "init", "--initial-branch=main")
    _git(source, "commit", "--allow-empty", "-m", "prod source")
    return _git(source, "rev-parse", "HEAD")


# meiva:note
# A root supervisor's control socket under `home`, answering `status` with this process as the
# root: the exact peer the client verifies, so the unit reads `supervisor_online` true.
#
# `home` must be short enough for a unix socket path (`short_tmp`). The server runs on its own
# event loop in a thread, the way a separate process would serve it.
@contextmanager
def root_supervisor_socket(home: Path) -> Generator[None]:
    owner = OwnedProcess.capture(psutil.Process())

    async def status(request: RequestPayload) -> ResponsePayload:
        assert request["verb"] == "status"
        return ok_response(
            {
                "root": {
                    "pid": owner.pid,
                    "create_time": owner.birth,
                    "starttime": owner.starttime,
                },
                "units": [],
            }
        )

    loop = asyncio.new_event_loop()
    server = ControlServer(root_run_dir(home) / "meiva-root.sock", status)
    started = threading.Event()

    def serve() -> None:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(server.start())
        started.set()
        loop.run_forever()
        loop.run_until_complete(server.close())

    thread = threading.Thread(target=serve, name="root-supervisor-socket")
    thread.start()
    try:
        assert started.wait(timeout=5.0), "the control socket did not start"
        yield
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()
