# meiva:note
# This host's snapshot in the multi-machine view.
#
# Reads the facts of this host once — pidfiles for the daemons, the session backends' records
# for the services and the agents' persistent shells, the database's local agent identities, the
# serving gate, the resource sample — and hands them to `report.assemble_status`, which decides
# what the snapshot says. It answers the cluster's observability endpoint.
#
#   Observability: GET /api/cluster/status — always returns this host's state directly.
#
# This is the package's composition root for the ambient reads a snapshot needs (the daemon
# endpoint table, `MEIVA_HOME`, the serving gate, the checkout HEAD, the cluster clock): each is
# read once here and travels down as a value. The `schema_mismatch` submodule supplies the
# snapshot's read-only migration diagnosis; `sessions` holds the session listing and the
# operations on an agent's persistent shells.

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg_pool import ConnectionPool

from base.clock import SYSTEM_TIMERS, Clock
from base.cluster.machine import MachineIdentityReader
from base.daemon.endpoints import ServiceEndpoints
from base.db import Database
from base.deploy.git.cluster_drift import checkout_head_sha
from base.deploy.lifecycle import start_serving
from base.host.env.registry import process_environment
from base.host.proc import process_alive
from base.host.resource_sample import ResourceSample, resource_sample
from base.native_process.loaded_commit import LoadedCommit
from base.sessions.backend import get_backend, get_shell_backend
from ops.cluster_status.model import (
    AGENT_SHELL_RE as AGENT_SHELL_RE,
)
from ops.cluster_status.report import (
    ClusterStatus as ClusterStatus,
)
from ops.cluster_status.report import (
    DbSnapshot,
    StatusFacts,
    assemble_status,
    unavailable_db_snapshot,
)
from ops.cluster_status.report import (
    serving_gate_reason as serving_gate_reason,
)
from ops.cluster_status.schema_mismatch import status as schema_mismatch_status
from ops.cluster_status.sessions import (
    ShellNotFoundError as ShellNotFoundError,
)
from ops.cluster_status.sessions import (
    agent_shell_sessions as agent_shell_sessions,
)
from ops.cluster_status.sessions import (
    capture_shell as capture_shell,
)
from ops.cluster_status.sessions import (
    collect_sessions,
)
from ops.cluster_status.sessions import (
    kill_agent_shells as kill_agent_shells,
)
from ops.cluster_status.sessions import (
    kill_shell as kill_shell,
)

_log = logging.getLogger(__name__)


# meiva:note
# The instant a snapshot is taken at, in the cluster's wall-clock zone (the host's own zone
# only when the process holds no authoritative cluster timezone). The ambient read of the
# zone setting lives here, once per snapshot; the listing rows take the instant as a value.
def cluster_now() -> datetime:
    return datetime.now().astimezone(Clock.from_settings().zone())


# meiva:note
# `connection(timeout=...)` overrides the pool's long-lived default for this probe. Status must
# degrade before the gateway's probe deadline when the central DB is down instead of queueing
# behind the pool for its full timeout.
#
# The bound covers the pool QUEUE only: once a connection is handed out, a query hung on a
# blackholed flow waits out the connection's own keepalive + statement ceiling (~60s,
# PG_KEEPALIVE_KWARGS / statement_timeout) before failing — same failure class as the
# pre-change fresh connects (connect timeout 5s, then the same ~60s query ceiling), so a
# blackhole slows the probe but never hangs it.
_POOL_BORROW_TIMEOUT_S = 2.0


# meiva:note
# Read a pidfile and test the pid it names for liveness. Returns (alive, pid).
#
# Missing/empty/non-int file -> (False, None). Pidfile present but the process is gone ->
# (False, pid).
def check_pidfile(pidfile_path: str) -> tuple[bool, int | None]:
    try:
        pid = int(Path(pidfile_path).read_text().strip())
    except (FileNotFoundError, ValueError):
        return False, None
    return process_alive(pid), pid


# meiva:note
# Count this machine's retained agent identities through the snapshot connection.
def count_local_agents(conn: psycopg.Connection, *, local_machine: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM agents_meta WHERE machine=%s AND status<>'terminated'",
        (local_machine,),
    ).fetchone()
    if row is None:
        raise RuntimeError("local agent count returned no row")
    return int(row[0])


# meiva:note
# Read the agent count and schema diagnosis through one snapshot-local connection.
#
# `ok` on the result says whether the read succeeded: a host whose database snapshot failed
# cannot claim readiness. During a data-plane outage the snapshot stays readable with no
# readiness claim and the zero-count default: the agent count and schema diagnosis share one
# bounded connection, and any failure of it degrades both together.
def read_db_snapshot(
    db: Database, pool: ConnectionPool | None, *, machine_name: str, count_agents: bool
) -> DbSnapshot:
    try:
        connection = (
            db.connect(autocommit=True)
            if pool is None
            else pool.connection(timeout=_POOL_BORROW_TIMEOUT_S)
        )
        with connection as conn:
            agent_count = (
                count_local_agents(conn, local_machine=machine_name) if count_agents else 0
            )
            schema_status = schema_mismatch_status(db, conn=conn, local_machine=machine_name)
        return DbSnapshot(ok=True, agent_count=agent_count, schema_mismatch=schema_status)
    except Exception as exc:
        _log.warning("database snapshot read failed; using degraded status", exc_info=True)
        return unavailable_db_snapshot(machine_name, type(exc).__name__)


# meiva:note
# One live resource sample, degraded to None on any psutil failure.
def read_resource_sample() -> ResourceSample | None:
    try:
        return resource_sample()
    except Exception:  # psutil may not be installed; degrade gracefully
        _log.warning("resource_sample failed (psutil missing?)", exc_info=True)
        return None


# meiva:note
# An observed native root is online; unavailable inspection stays unknown.
def _supervisor_online(home: Path) -> bool | None:
    from base.native_process.root_control.client import RootClientError, root_process

    try:
        return root_process(home=home) is not None
    except (RootClientError, RuntimeError):
        return None


# meiva:note
# Assemble this host's cluster state — used by `/api/cluster/status`.
#
# When setup is missing, the supplied machine owner's independent name and role readers raise
# specific exceptions; this function passes them through and FastAPI surfaces as default 500
# (admin endpoint, not consumed by SDK).
def status_snapshot(
    db: Database,
    pool: ConnectionPool | None = None,
    *,
    image: LoadedCommit,
    local_machine: MachineIdentityReader,
    home: Path,
) -> ClusterStatus:
    machine_name = local_machine.name()
    roles = local_machine.role()
    runner = "agent-runner" in roles
    head_sha = checkout_head_sha(home / "source", environ=process_environment())
    agent_host_online = (
        check_pidfile(str(ServiceEndpoints.from_settings().of("agent_host").pidfile))[0]
        if runner
        else None
    )
    supervisor_online = _supervisor_online(home)
    sessions = collect_sessions(
        service_backend=get_backend(timers=SYSTEM_TIMERS),
        shell_backend=get_shell_backend(),
        now=cluster_now(),
    )
    # meiva:note
    # The live one-shot resource sample blocks for its CPU interval. Run it in parallel with
    # the central-DB reads so snapshot latency pays the slower of those independent
    # operations, not their sum.
    with ThreadPoolExecutor(max_workers=1) as executor:
        resource_future = executor.submit(read_resource_sample)
        db_snapshot = read_db_snapshot(db, pool, machine_name=machine_name, count_agents=runner)
        resource = resource_future.result()
    return assemble_status(
        StatusFacts(
            machine_name=machine_name,
            roles=roles,
            head_sha=head_sha,
            running_sha=image.sha,
            sessions=sessions,
            agent_host_online=agent_host_online,
            supervisor_online=supervisor_online,
            db=db_snapshot,
            resource=resource,
            serving=start_serving.is_serving(home),
        )
    )
