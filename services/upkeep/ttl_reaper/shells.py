"""Reclaim TTL-expired persistent shell sessions on their home machines.

The 2026-08-27 ruling makes a TTL mandatory for every persistent shell session
created via ``meiva.shell.sessions.new(ttl=)`` / ``run_background(ttl=)``; the TTL
is the only reclamation mechanism. Every session — a plain shell or a watcher
(``meiva.watcher.at/cron/launch``), which is just a shell session running a
generated script with no registry or deadline of its own
(docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md) — carries one
``agent_shell_ttls`` row, and every expired row is reclaimed the same way: a
``shell_kill`` op to the owning agent's machine, then the row is deleted.

The row is deleted only on a definitive verdict (killed / absent /
machine_absent); an unreachable machine or a version-skewed runner leaves it for
the next round — deleting the row would orphan the live session. A machine
absent from the machines registry is definitive too (nothing on it can be dialed
again under that name — task #4143): the row terminalizes with the
``machine_absent`` verdict instead of deferring forever.

One round groups the expired rows by home machine: machines are reclaimed
concurrently, the rows of one machine one after another, so a slow machine holds
up only its own rows. Each dispatch runs under a deadline sized from the RPC
client's own budget.

The owner's ``meiva.shell.sessions.renew()`` extends a live session's deadline
before it passes; each kill dispatch re-checks the row is still expired first
(renewal's own ``expires_at > clock_timestamp()`` guard makes the pair airtight),
so a renewed session is never killed.

Owners are notified (inbound, source ``"system"``) only when the reap interrupted
a running job (the runner reports whether the session carried live processes at
kill time); an empty shell's reaping is silent, and an already-absent session
never notifies. A watcher session always carries a running job, so reclaiming
one notifies every time. An interruption notice states when the TTL expired and
how long it was.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from datetime import timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from base import telemetry
from base.daemon import round_loop
from base.daemon.loop_health import LoopProgress
from base.db import Database
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from ops.cluster import rpc as cluster_rpc
from services.upkeep.ttl_reaper import shell_plan, stale_plan
from services.upkeep.ttl_reaper.config import TtlReaperConfig
from services.upkeep.ttl_reaper.owner_notice import (
    PASS_BATCH,
    OwnerMessage,
    notify_owner,
    wake_owners,
)
from services.upkeep.ttl_reaper.shell_plan import Settlement, ShellRow

_log = logging.getLogger(__name__)

# meiva:note
# Per-attempt dispatch budget: a reachable runner answers a shell op (kill or probe) in
# milliseconds; an unreachable one fails the connect within this bound. `_MACHINE_CONCURRENCY` is
# how many machines one round works on at once.
_SHELL_OP_TIMEOUT_S = 5.0
_MACHINE_CONCURRENCY = 8

# meiva:note
# How many remote rounds pass between two stale sweeps.
#
# A candidate row is any unexpired row older than `STALE_ROW_GRACE`, so a healthy cluster always
# has some — every long-lived session contributes one — and one probe per (machine, agent) every
# round would spend a runner's session listing once a minute on a table that changes rarely (24
# pairs on 3 machines at the 2026-10-27 reading). One sweep per ten rounds keeps the post-backlog
# drain at ten minutes per batch of `PASS_BATCH` rows and costs a fortieth of that.
SWEEP_EVERY_ROUNDS = 10

# meiva:note
# How long a tracking row is exempt from the stale sweep after its own creation. The probe answer
# is a listing read at one instant, and a session's row may exist a moment before the host's
# backend can list the session name, so a young row is never evidence of a dead session. Six hours
# is far longer than any such gap: a session that dies now is swept within six hours of its row's
# birth, and any older row is swept on the first round after its session is gone.
STALE_ROW_GRACE = timedelta(hours=6)


# meiva:note
# Overall deadline for one shell op dispatch (a kill, or a probe of the stale sweep): the RPC
# client's worst case at the per-attempt budget, so it only cuts a dispatch the client's own
# timeout and retry budgets did not. The daemon reads it once into
# `TtlReaperConfig.shell_kill_deadline_s`.
def dispatch_deadline_s() -> float:
    return cluster_rpc.worst_case_dispatch_seconds(
        cluster_rpc.rpc_config_from_settings(), _SHELL_OP_TIMEOUT_S
    )


# meiva:note
# TTL-expired shell tracking rows with their owner's home machine, oldest deadline first.
#
# Schedule sessions never carry rows at all (they have no agent id; the ScheduleManager reaps
# them, see ``services/wake/schedule_manager/manager.py`` ``_launch``). Each row carries
# ``expires_at`` and ``created_at`` so the interruption notice can state when the TTL expired
# and how long it was.
def expired_shell_rows_blocking(pool: ConnectionPool) -> list[ShellRow]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT t.agent_id, t.session_id, t.expires_at, t.created_at, m.machine "
            "FROM agent_shell_ttls t LEFT JOIN agents_meta m ON m.id = t.agent_id "
            "WHERE t.expires_at <= clock_timestamp() "
            "ORDER BY t.agent_id, t.session_id LIMIT %s",
            (PASS_BATCH,),
        )
        return [ShellRow(**row) for row in cur.fetchall()]


def claim_still_expired(cur: psycopg.Cursor[Any], agent_id: int, session_id: int) -> bool:
    """The claim UPDATE; True only while the row is STILL expired.

    The guard compares against ``clock_timestamp()`` — the statement time,
    evaluated after the row lock is acquired — never ``now()`` (transaction
    start). The claim transaction can begin long before the deadline while it
    waits on a busy row; with ``now()`` it would skip a row that expired after
    its transaction started, and it would not serialize correctly against a
    renewal whose own guard also evaluates at statement time (issue #2053).
    """
    cur.execute(
        "UPDATE agent_shell_ttls SET expires_at = expires_at "
        "WHERE agent_id = %s AND session_id = %s AND expires_at <= clock_timestamp()",
        (agent_id, session_id),
    )
    return cur.rowcount == 1


def claim_shell_row_still_expired(pool: ConnectionPool, agent_id: int, session_id: int) -> bool:
    """Re-verify one expired shell row just before dispatching its kill.

    The expired-row select and the kill dispatch are separated by the other
    rows of the machine, and the owner's ``sessions.renew()`` may land in
    between. A no-op UPDATE claims the row only while it is STILL expired
    (rowcount 1); a renewal in the gap makes the claim fail and the round skips
    the kill — the row is no longer expired and would not be selected next round
    either. Combined with renewal's ``expires_at > clock_timestamp()`` write
    guard the pair is airtight: whichever of the claim and the renewal acquires
    the row lock first wins, and the loser re-evaluates at its own statement
    time.
    """
    with write_transaction(pool) as conn, conn.cursor() as cur:
        return claim_still_expired(cur, agent_id, session_id)


# meiva:note
# Drop a reclaimed shell's tracking row and queue `notice` for its owner when there is one.
#
# The caller decides the text (`shell_plan.reclaim_notice`): an empty shell's reaping is silent,
# a reaped watcher session reads like any other interrupted job. The owner is woken only after
# the row is deleted and the transaction committed.
def delete_shell_row(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    agent_id: int,
    session_id: int,
    *,
    notice: str | None,
) -> None:
    queued: list[OwnerMessage] = []
    with write_transaction(pool) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s",
                (agent_id, session_id),
            )
        if notice is not None:
            message = notify_owner(conn, agent_id, notice)
            if message is not None:
                queued.append(message)
    wake_owners(db, bus, queued)


# meiva:note
# The one place a shell op leaves this process: `shell_kill` and `shell_probe` share the per-attempt
# budget, the dispatch deadline and the transport errors, so both verdicts are read from one call
# site. Failures travel to the caller unchanged — what an unanswered dial means differs between the
# two ops and stays with each.
async def _dispatch_shell_op(
    db: Database, machine: str, kind: str, payload: dict[str, Any], *, deadline_s: float
) -> dict[str, Any]:
    async with asyncio.timeout(deadline_s):
        return await cluster_rpc.dispatch_to_machine(
            db, machine, kind, payload, timeout_s=_SHELL_OP_TIMEOUT_S
        )


# meiva:note
# One ``shell_kill`` dispatch settled into a verdict, or None for "defer to the next round".
#
# A machine absent from the machines registry (task #4143) cannot be dialed: the row
# terminalizes with the distinct ``machine_absent`` verdict, because the live-host ``absent``
# means the host answered that the session already ended, while no host exists here to answer
# at all. Deferring would retry the row forever. An unreachable machine, a failed op, a
# malformed answer or a dispatch that outlives `deadline_s` defers: the session may still live.
async def dispatch_shell_kill(
    db: Database, machine: str, agent_id: int, session_id: int, *, deadline_s: float
) -> Settlement | None:
    try:
        answer = await _dispatch_shell_op(
            db,
            machine,
            "shell_kill",
            {"agent_id": agent_id, "session_id": session_id},
            deadline_s=deadline_s,
        )
    except cluster_rpc.ClusterOpTargetAbsent:
        _log.info(
            "[ttl-reaper] shell %s of agent %s: machine %r is absent from the "
            "registry — terminalizing with the machine_absent verdict",
            session_id,
            agent_id,
            machine,
        )
        return shell_plan.machine_absent_settlement()
    except (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed, TimeoutError) as exc:
        _log.warning(
            "[ttl-reaper] shell_kill for agent %s session %s deferred: %r",
            agent_id,
            session_id,
            exc,
        )
        return None
    settlement = shell_plan.settle(answer)
    if settlement is None:
        _log.warning(
            "[ttl-reaper] shell_kill for agent %s session %s returned %r",
            agent_id,
            session_id,
            answer,
        )
    return settlement


# meiva:note
# Reclaim one expired shell on `machine`: the (agent, session) when its row was settled, None
# when the row is left for the next round.
async def reclaim_row(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    machine: str,
    shell: ShellRow,
    config: TtlReaperConfig,
) -> tuple[int, int] | None:
    agent_id = shell.agent_id
    session_id = shell.session_id
    if not await asyncio.to_thread(claim_shell_row_still_expired, pool, agent_id, session_id):
        # Renewed between the select and this dispatch — the deadline moved
        # forward, so the session is no longer reaper business.
        _log.info("[ttl-reaper] shell %s of agent %s was renewed — skipping", session_id, agent_id)
        return None
    settlement = await dispatch_shell_kill(
        db, machine, agent_id, session_id, deadline_s=config.shell_kill_deadline_s
    )
    if settlement is None:
        return None
    notice = shell_plan.reclaim_notice(
        shell,
        settlement,
        zone=config.zone,
        today=config.clock.now().astimezone(config.zone).date(),
    )
    await asyncio.to_thread(delete_shell_row, pool, db, bus, agent_id, session_id, notice=notice)
    telemetry.emit(
        "log",
        "shell_ttl_expired",
        level="info",
        agent_id=agent_id,
        attributes={
            "agent_id": agent_id,
            "session_id": session_id,
            "mode": settlement.verdict,
            "interrupted": settlement.interrupted,
        },
    )
    return agent_id, session_id


async def reap_expired_shells(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    progress: LoopProgress,
    config: TtlReaperConfig,
) -> list[tuple[int, int]]:
    """Kill every TTL-expired shell session on its home machine; return the
    (agent, session) pairs whose rows were settled.

    Machines run concurrently, the rows of one machine in order. A row whose
    owner has no usable home machine is left for the next round. All DB work
    runs via to_thread: the event loop never blocks on psycopg.
    """
    plan = shell_plan.plan_round(await asyncio.to_thread(expired_shell_rows_blocking, pool))
    for shell in plan.unplaced:
        _log.warning(
            "[ttl-reaper] shell %s of agent %s has unknown machine — deferring",
            shell.session_id,
            shell.agent_id,
        )
    reaped: list[tuple[int, int]] = []

    async def reclaim_machine(batch: shell_plan.MachineBatch) -> None:
        for shell in batch.shells:
            settled = await reclaim_row(pool, db, bus, batch.machine, shell, config)
            if settled is not None:
                reaped.append(settled)
            progress.beat()

    await round_loop.fan_out(
        [functools.partial(reclaim_machine, batch) for batch in plan.batches],
        concurrency=_MACHINE_CONCURRENCY,
        progress=progress,
    )
    return reaped


# meiva:note
# Unexpired tracking rows old enough for the stale sweep, one batch per pass, with their owner's
# home machine.
#
# These are the rows no reaper phase reaches by deadline: `expires_at <= now()` belongs to the
# kill path above, and the sweep is the only reader of the rest. A row whose owner has no usable
# machine is filtered out here rather than returned as unplaced — it cannot be swept at all, and
# letting it into the batch would fill every pass with rows that can never settle.
def stale_shell_rows_blocking(pool: ConnectionPool) -> list[ShellRow]:
    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT t.agent_id, t.session_id, t.expires_at, t.created_at, m.machine "
            "FROM agent_shell_ttls t LEFT JOIN agents_meta m ON m.id = t.agent_id "
            "WHERE t.expires_at > clock_timestamp() "
            "AND t.created_at < clock_timestamp() - %s "
            "AND m.machine IS NOT NULL AND m.machine <> %s "
            "ORDER BY t.agent_id, t.session_id LIMIT %s",
            (STALE_ROW_GRACE, shell_plan.UNKNOWN_MACHINE, PASS_BATCH),
        )
        return [ShellRow(**row) for row in cur.fetchall()]


# meiva:note
# The live session ids a machine's runner lists for `agent_id`, or None when the answer proves
# nothing.
#
# A machine absent from the machines registry cannot be dialed ever again: an empty set is the
# honest verdict there, the same `machine_absent` reading the kill path takes. An unreachable
# host, a failed op, a dispatch past `deadline_s` or a malformed answer defers the whole group —
# the session may still live. The probe is authoritative when it answers: the host enumerates its
# session backend and raises rather than reporting a failed listing as an empty one.
async def dispatch_shell_probe(
    db: Database, machine: str, agent_id: int, *, deadline_s: float
) -> frozenset[int] | None:
    try:
        answer = await _dispatch_shell_op(
            db, machine, "shell_probe", {"agent_id": agent_id}, deadline_s=deadline_s
        )
    except cluster_rpc.ClusterOpTargetAbsent:
        _log.info(
            "[ttl-reaper] stale sweep of agent %s: machine %r is absent from the registry — "
            "its rows cannot be dialed again",
            agent_id,
            machine,
        )
        return frozenset()
    except (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed, TimeoutError) as exc:
        _log.warning(
            "[ttl-reaper] stale sweep of agent %s on %r deferred: %r", agent_id, machine, exc
        )
        return None
    live = stale_plan.probe_session_ids(answer)
    if live is None:
        _log.warning(
            "[ttl-reaper] shell_probe for agent %s on %r returned %r", agent_id, machine, answer
        )
    return live


# meiva:note
# Drop one tracking row — but only the row this round judged. `expires_at` and `created_at` are
# part of the predicate, so a row renewed, re-created for a new session that reused the id, or
# otherwise rewritten since the select is left alone; the loser of that race keeps its deadline.
def delete_stale_row(pool: ConnectionPool, row: ShellRow) -> bool:
    with write_transaction(pool) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM agent_shell_ttls WHERE agent_id = %s AND session_id = %s "
            "AND expires_at = %s AND created_at = %s",
            (row.agent_id, row.session_id, row.expires_at, row.created_at),
        )
        return cur.rowcount == 1


# meiva:note
# Drop the tracking rows of sessions that no longer exist on their home machine.
#
# A deadline is not the only way a row becomes garbage: a session that dies with its host (a tree
# restart, a crashed runner) leaves its row behind until the declared deadline, and no phase reads
# an unexpired row — a watcher that declares its TTL a year out would keep its row for that year
# (task #5662). One `shell_probe` per (machine, agent), never one per row, asks the host which
# sessions it still lists; machines run concurrently and the agents of one machine in order, up to
# the first probe that gets no answer. Only a definitive answer settles a row: an unreachable host,
# a failed op, a dispatch past its deadline or a malformed listing leaves every row alone, since
# dropping a live session's row would leave that session unreclaimable forever. One batch of rows
# per round bounds the pass, so a backlog drains over several rounds instead of inside one long
# transaction. All DB work runs via to_thread: the event loop never blocks on psycopg.
async def reap_stale_shells(
    pool: ConnectionPool, db: Database, progress: LoopProgress, config: TtlReaperConfig
) -> list[tuple[int, int]]:
    plan = stale_plan.plan_stale(await asyncio.to_thread(stale_shell_rows_blocking, pool))
    for row in plan.unplaced:
        _log.warning(
            "[ttl-reaper] stale sweep: shell %s of agent %s has unknown machine — deferring",
            row.session_id,
            row.agent_id,
        )
    dropped: list[tuple[int, int]] = []

    async def sweep_machine(probes: stale_plan.MachineProbes) -> None:
        for agent in probes.agents:
            live = await dispatch_shell_probe(
                db, probes.machine, agent.agent_id, deadline_s=config.shell_kill_deadline_s
            )
            if live is None:
                # meiva:note
                # The host did not answer for one agent, so it will not answer for the next one in
                # this round either: its remaining rows wait instead of repeating the failure.
                return
            for row in stale_plan.stale_rows(agent.rows, live):
                if not await asyncio.to_thread(delete_stale_row, pool, row):
                    continue
                dropped.append((row.agent_id, row.session_id))
                telemetry.emit(
                    "log",
                    "shell_ttl_stale_dropped",
                    level="info",
                    agent_id=row.agent_id,
                    attributes={
                        "agent_id": row.agent_id,
                        "session_id": row.session_id,
                        "machine": probes.machine,
                    },
                )

    await round_loop.fan_out(
        [functools.partial(sweep_machine, probes) for probes in plan.machines],
        concurrency=_MACHINE_CONCURRENCY,
        progress=progress,
    )
    return dropped
