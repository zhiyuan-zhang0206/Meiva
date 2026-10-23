# meiva:note
# The roster's read model: the last status_probe of each roster-visible agent-runner.
#
# The heartbeat service's liveness pass probes every such machine once a minute and
# writes the outcome into `machine_status_snapshot` (`services/wake/heartbeat/liveness.py`).
# The gateway's roster and machines reads render from these rows instead of dialing
# each runner on every read, so a blackholed host can never cost a read its dial
# budget (task #3507) and the gateway keeps no failure memory of its own. A row older
# than `MAX_AGE_S` (the heartbeat service is down or wedged), or no row, is not
# evidence of anything: the reader dials that machine itself, as an explicit fresh
# read would. Splitting reads from dials is the whole point, so the rows carry their
# age and the roster renders it (`MachineStatus.observed_at`).
#
# This module is the reads; `gateway.cluster.roster.liveness_snapshot` is the row and its verdicts,
# and `gateway.cluster.roster.roster_plan` decides what the roster does with them.

from __future__ import annotations

from typing import Any

from psycopg import Cursor
from psycopg_pool import ConnectionPool

from gateway.cluster.roster.liveness_snapshot import Snapshot
from gateway.cluster.roster.roster_rows import RosterRowSpec


# meiva:note
# Every machine's snapshot, by machine name.
def read_all(cur: Cursor[Any]) -> dict[str, Snapshot]:
    cur.execute(
        "SELECT machine_name, observed_at, reachable, consecutive_failures, status, status_at "
        "FROM machine_status_snapshot"
    )
    return {
        str(name): Snapshot(observed, bool(reachable), int(failures), status, status_at)
        for name, observed, reachable, failures, status, status_at in cur.fetchall()
    }


# meiva:note
# Sync read of every snapshot — via to_thread.
def read_all_blocking(pool: ConnectionPool[Any]) -> dict[str, Snapshot]:
    with pool.connection() as conn, conn.cursor() as cur:
        return read_all(cur)


# meiva:note
# The unpaused machines, by name — paused rows are out of the cluster; `meiva cluster
# resume` brings one back.
def readroster_rows(cur: Cursor[Any]) -> list[RosterRowSpec]:
    cur.execute(
        "SELECT name, gateway_url, role, up_since_at, description, stopped_at, is_staging "
        "FROM machines WHERE paused_at IS NULL ORDER BY name"
    )
    return [RosterRowSpec(*row) for row in cur.fetchall()]


# meiva:note
# Sync machines-table read — via to_thread (used by roster + machines).
def readroster_rows_blocking(pool: ConnectionPool[Any]) -> list[RosterRowSpec]:
    with pool.connection() as conn, conn.cursor() as cur:
        return readroster_rows(cur)
