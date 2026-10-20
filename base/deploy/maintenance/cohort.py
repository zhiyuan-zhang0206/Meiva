# meiva:note
# Durable restart cohort for an already admitted hosted runner.
#
# Preparation and admission serialize on the existing agents_meta row lock.
# The local journal is published first; once it is held, admission lets through
# only a cohort member's pending restart. No lifecycle target, acknowledgement, or
# checkpoint is invented here.

from datetime import datetime
from typing import NamedTuple
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from base import telemetry
from base.deploy.maintenance import admission, pause_owner
from base.deploy.maintenance.state import MaintenanceHold, MaintenancePhase


class LifecycleCollisionError(RuntimeError):
    """Preparation met unfinished lifecycle work it did not author.

    Raised before the cohort is frozen (task #3591) so a bounded retry has a
    clean boundary: a collision leaves the journal untouched, and the retry
    re-derives the cohort from the resolved world under the same row locks.
    ``waitable`` is True only when every competing row carries no maintenance
    payload: an ordinary agent lifecycle operation (``restart``/``terminate``)
    and any parked claims left after orphan settlement enter the bounded wait;
    maintenance-authored commands keep their refuse-now semantics, and a mix
    refuses too.
    """

    def __init__(self, detail: str, agent_ids: list[int], *, waitable: bool) -> None:
        super().__init__(detail)
        self.agent_ids = tuple(agent_ids)
        self.waitable = waitable


# meiva:note
# Freeze the original runnable set and enqueue one restart per member.
#
# A failure leaves the hold in place. Repeating preparation resumes the
# same cohort and finds already committed commands by the exact operation.
# Terminated agents never enter the cohort. A previous lifecycle operation
# requires separate resolution. Ordinary orphan claims
# on parked agents are consumed (marked done, never redelivered)
# before the collision guards run.
#
# An unfinished agent lifecycle command belonging to another actor raises
# ``LifecycleCollisionError`` before the cohort is frozen; the caller may
# bounded-wait and retry, and the retry re-verifies everything under the
# same row locks (task #3591).
def prepare(
    conn: psycopg.Connection,
    *,
    machine: str,
    host_owner: UUID | None,
    holder: str,
    acquired_at: datetime,
) -> MaintenanceHold:
    current = admission.require_operation(holder, acquired_at)
    hold = current.maintenance
    assert hold is not None  # noqa: S101
    if hold.phase != MaintenancePhase.PREPARING:
        return hold
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("maintenance preparation requires an idle connection it can commit")
    with conn.transaction():
        conn.execute("SET TRANSACTION READ WRITE")
        conn.execute("SET LOCAL lock_timeout='5s'")
        rows = conn.execute(
            "SELECT id,status,runtime_owner,runtime_generation FROM agents_meta "
            "WHERE machine=%s AND status<>'terminated' ORDER BY id FOR UPDATE",
            (machine,),
        ).fetchall()
        applied = applied_capture(conn, hold, holder, acquired_at)
        captured = classify([RuntimeRow(*row) for row in rows], hold, host_owner, applied)
        settled = _settle_orphan_claims(conn, captured)
        refuse_inflight_lifecycle(conn, captured, holder=holder, acquired_at=acquired_at)
        require_resolved(conn, captured)
        if captured != hold:
            pause_owner.change_maintenance(holder, acquired_at, hold, captured)
            hold = captured
        commands: dict[int, int] = {}
        for agent_id in sorted(hold.commands):
            commands[agent_id] = _restart(conn, agent_id, holder, acquired_at)
    _emit_orphan_settlements(settled)
    draining = MaintenanceHold(MaintenancePhase.DRAINING, commands, parked=hold.parked)
    pause_owner.change_maintenance(holder, acquired_at, hold, draining)
    return draining


class RuntimeRow(NamedTuple):
    agent_id: int
    status: str
    owner: UUID | None
    generation: UUID | None

    # meiva:note
    # An idle agent owned by nobody or by a host boot other than the one being
    # restarted.
    #
    # A departed host boot leaves its idle agents with the owner; the next wake
    # re-admits them under the new boot's generation. Nothing is running for a
    # parked agent, so maintenance leaves it untouched.
    def parked_idle(self, host_owner: UUID | None) -> bool:
        return (
            self.status == "idling"
            and (self.owner is None or self.owner != host_owner)
            and (self.owner is None) == (self.generation is None)
        )

    def active_for(self, owner: UUID | None) -> bool:
        return (
            self.status in ("running", "idling")
            and self.owner == owner
            and self.generation is not None
        )


class OrphanClaim(NamedTuple):
    """An ordinary claimed inbound on a parked, runtime-less agent."""

    agent_id: int
    message_id: int
    age_s: float


# meiva:note
# Read the ordinary claims eligible for preparation's orphan settlement.
#
# Preparation supplies its locked, classified parked set.
def orphaned_claims(conn: psycopg.Connection, *, parked: tuple[int, ...]) -> list[OrphanClaim]:
    agents = sorted(set(parked))
    if not agents:
        return []
    return [
        OrphanClaim(*row)
        for row in conn.execute(
            "SELECT agent_id,id,"
            "EXTRACT(EPOCH FROM (now()-COALESCE(claimed_at,created_at)))::double precision "
            "FROM inbound_messages WHERE agent_id=ANY(%s) AND status='claimed' "
            "AND payload->'maintenance' IS NULL AND kind NOT IN ('restart','terminate') "
            "ORDER BY agent_id,id",
            (agents,),
        ).fetchall()
    ]


# meiva:note
# Consume orphaned ordinary claims under preparation's agent row locks.
#
# A claim whose runtime is gone is marked `done`, never returned to
# `pending`: like a host restart, preparation does not redeliver input a dead
# runtime had taken. Returns only the rows this transaction changed.
def _settle_orphan_claims(conn: psycopg.Connection, hold: MaintenanceHold) -> list[OrphanClaim]:
    consumed: list[OrphanClaim] = []
    for row in orphaned_claims(conn, parked=hold.parked):
        changed = conn.execute(
            "UPDATE inbound_messages SET status='done' WHERE id=%s AND status='claimed'",
            (row.message_id,),
        ).rowcount
        if changed:
            consumed.append(row)
    return consumed


def _emit_orphan_settlements(consumed: list[OrphanClaim]) -> None:
    """Report only committed CAS changes, never a rolled-back preparation attempt."""
    for row in consumed:
        telemetry.emit(
            "telemetry",
            "pause_orphan_claim_settled",
            agent_id=row.agent_id,
            attributes={
                "agent": row.agent_id,
                "message_id": row.message_id,
                "age_s": round(row.age_s, 3),
            },
        )


def classify(
    rows: list[RuntimeRow],
    hold: MaintenanceHold,
    owner: UUID | None,
    applied: set[int],
) -> MaintenanceHold:
    parked = _parked_agents(rows, hold, owner)
    captured = bool(hold.commands or hold.parked)
    if captured and hold.parked != parked:
        raise RuntimeError("parked native intent changed while preparing")
    candidates = set(hold.commands) if captured else {row.agent_id for row in rows} - set(parked)
    selected = [row for row in rows if row.agent_id in candidates]
    if {row.agent_id for row in selected} != candidates:
        raise RuntimeError("maintenance cohort changed lifecycle while preparing")
    _require_live_owners(selected, owner, applied)
    return (
        hold
        if captured
        else MaintenanceHold(commands=dict.fromkeys(sorted(candidates), 0), parked=parked)
    )


def _parked_agents(
    rows: list[RuntimeRow], hold: MaintenanceHold, host_owner: UUID | None
) -> tuple[int, ...]:
    """Agents outside the cohort whose native intent is parked (idle, no live owner)."""
    return tuple(
        sorted(
            row.agent_id
            for row in rows
            if row.agent_id not in hold.commands and row.parked_idle(host_owner)
        )
    )


def _require_live_owners(selected: list[RuntimeRow], owner: UUID | None, applied: set[int]) -> None:
    invalid = [
        row.agent_id
        for row in selected
        if not row.active_for(owner) and row.agent_id not in applied
    ]
    if invalid:
        raise RuntimeError(f"maintenance requires the live original native owner: {invalid}")


def applied_capture(
    conn: psycopg.Connection, hold: MaintenanceHold, holder: str, acquired_at: datetime
) -> set[int]:
    # meiva:note
    # The original host can execute a committed restart before the journal's
    # final write succeeds. Preserve this cohort on retry: the restart row is
    # done, it carries the release marker, and it names this exact operation.
    operation = Jsonb({"holder": holder, "acquired_at": acquired_at.isoformat()})
    rows = conn.execute(
        "SELECT m.id FROM agents_meta m JOIN inbound_messages i "
        "ON i.agent_id=m.id "
        "WHERE m.id=ANY(%s) AND m.status='idling' AND m.runtime_owner IS NULL "
        "AND m.runtime_generation IS NULL "
        "AND i.kind='restart' AND i.source='system:restart' AND i.status='done' "
        "AND COALESCE((i.payload -> 'lifecycle_release') = 'true'::jsonb, false) "
        "AND i.payload->'maintenance'=%s",
        (list(hold.commands), operation),
    ).fetchall()
    return {row[0] for row in rows}


def require_resolved(conn: psycopg.Connection, hold: MaintenanceHold) -> None:
    """The second guard on the same collision, with the same waitability rule.

    ``refuse_inflight_lifecycle`` runs first and already covers every parked
    row it finds unresolved; this repeats the parked half independently so a
    change to either query cannot silently skip parked agents (task #3591). It
    raises the same typed collision: parked rows without a maintenance payload
    enter the bounded wait; maintenance-authored rows refuse immediately — and
    preparation still never freezes past unresolved parked claims.
    """
    rows = _open_commands(conn, list(hold.parked))
    unresolved = [row for row in rows if _unresolved_parked(row)]
    if not unresolved:
        return
    raise LifecycleCollisionError(
        "; ".join(_collision_line(row.agent_id, [row]) for row in unresolved),
        sorted({row.agent_id for row in unresolved}),
        waitable=all(row.maintenance is None for row in unresolved),
    )


class _CommandRow(NamedTuple):
    """One pending/claimed inbound command relevant to a collision decision."""

    agent_id: int
    command_id: int
    kind: str
    status: str
    maintenance: object


def _open_commands(conn: psycopg.Connection, agents: list[int]) -> list[_CommandRow]:
    """Every pending/claimed inbound command of `agents`, ordered by agent then id."""
    return [
        _CommandRow(*row)
        for row in conn.execute(
            "SELECT agent_id, id, kind, status, payload->'maintenance' "
            "FROM inbound_messages WHERE agent_id=ANY(%s) AND status IN ('pending','claimed') "
            "ORDER BY agent_id, id",
            (agents,),
        ).fetchall()
    ]


def _member_collisions(
    rows: list[_CommandRow], hold: MaintenanceHold, operation: dict[str, str]
) -> list[tuple[int, list[_CommandRow]]]:
    """Cohort members with a lifecycle command other than this maintenance operation's own."""
    collisions: list[tuple[int, list[_CommandRow]]] = []
    for agent_id in sorted(hold.commands):
        member = [
            row for row in rows if row.agent_id == agent_id and row.kind in ("restart", "terminate")
        ]
        if not member or (len(member) == 1 and member[0].maintenance == operation):
            continue
        collisions.append((agent_id, member))
    return collisions


def _parked_collisions(
    rows: list[_CommandRow], hold: MaintenanceHold
) -> list[tuple[int, list[_CommandRow]]]:
    """Parked agents' commands that are still unresolved."""
    return [
        (agent_id, [row])
        for agent_id in sorted(hold.parked)
        for row in rows
        if row.agent_id == agent_id and _unresolved_parked(row)
    ]


def refuse_inflight_lifecycle(
    conn: psycopg.Connection,
    hold: MaintenanceHold,
    *,
    holder: str,
    acquired_at: datetime,
) -> None:
    """Declare unfinished lifecycle work before the cohort freezes (task #3591).

    Runs inside preparation's row-locked transaction but BEFORE the capture is
    persisted: a ``LifecycleCollisionError`` then leaves the journal untouched, so
    the caller's bounded retry starts from the same clean boundary and
    re-verifies every condition under the same locks. This mirrors the two
    downstream refusal checks — the per-member pending-command check in
    ``_restart`` and the parked-agent predicate of ``require_resolved`` — one
    step earlier, carrying the waitability the retry decision needs: ordinary
    work without a maintenance payload is waitable — both an in-flight agent
    lifecycle operation (restart/terminate) and any parked claim left after
    orphan settlement; maintenance-authored commands are not.
    """
    agents = sorted(set(hold.commands) | set(hold.parked))
    if not agents:
        return
    rows = _open_commands(conn, agents)
    operation = {"holder": holder, "acquired_at": acquired_at.isoformat()}
    collisions = _member_collisions(rows, hold, operation)
    collisions.extend(_parked_collisions(rows, hold))
    if not collisions:
        return
    raise LifecycleCollisionError(
        "; ".join(_collision_line(agent_id, found) for agent_id, found in collisions),
        sorted({agent_id for agent_id, _ in collisions}),
        waitable=all(row.maintenance is None for _, found in collisions for row in found),
    )


def _collision_line(agent_id: int, rows: list[_CommandRow]) -> str:
    detail = ", ".join(f"{row.kind} {row.command_id} ({row.status})" for row in rows)
    lifecycle = all(row.kind in ("restart", "terminate") for row in rows)
    problem = "another unfinished lifecycle command" if lifecycle else "unresolved claimed work"
    return f"agent {agent_id} has {problem}: {detail}"


def _unresolved_parked(row: _CommandRow) -> bool:
    """`require_resolved`'s predicate, per row, for the waitability decision."""
    return row.status == "claimed" or row.kind in ("restart", "terminate")


def _restart(conn: psycopg.Connection, agent_id: int, holder: str, acquired_at: datetime) -> int:
    operation = {"holder": holder, "acquired_at": acquired_at.isoformat()}
    pending = conn.execute(
        "SELECT id,payload->'maintenance' FROM inbound_messages WHERE agent_id=%s "
        "AND kind IN ('restart','terminate') AND status IN ('pending','claimed') "
        "ORDER BY id FOR UPDATE",
        (agent_id,),
    ).fetchall()
    if pending:
        if len(pending) != 1 or pending[0][1] != operation:
            raise RuntimeError(f"agent {agent_id} has another unfinished lifecycle command")
        return pending[0][0]
    # meiva:note
    # A retry finds the restart this operation already had executed.
    executed = conn.execute(
        "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='restart' "
        "AND source='system:restart' AND status='done' AND payload->'maintenance'=%s "
        "ORDER BY id DESC LIMIT 1",
        (agent_id, Jsonb(operation)),
    ).fetchone()
    if executed is not None:
        return executed[0]
    row = conn.execute(
        "INSERT INTO inbound_messages(agent_id,kind,source,content,payload) "
        "VALUES(%s,'restart','system:restart','',%s) RETURNING id",
        (agent_id, Jsonb({"maintenance": operation})),
    ).fetchone()
    assert row is not None  # noqa: S101
    return row[0]


# meiva:note
# Cross-check local continuation receipts against the executed restart rows.
def verify_drained(conn: psycopg.Connection, hold: MaintenanceHold) -> None:
    if set(hold.drained) != set(hold.commands):
        raise RuntimeError("maintenance still has unfinished continuations")
    if hold.parked:
        rows = conn.execute(
            "SELECT id,status,runtime_owner,runtime_generation FROM agents_meta WHERE id=ANY(%s)",
            (list(hold.parked),),
        ).fetchall()
        parked = [RuntimeRow(*row) for row in rows]
        if {row.agent_id for row in parked if row.parked_idle(None)} != set(hold.parked):
            raise RuntimeError("parked agent intent changed during maintenance")
    for agent_id, command_id in hold.commands.items():
        row = conn.execute(
            "SELECT 1 FROM agents_meta m JOIN inbound_messages i "
            "ON i.agent_id=m.id WHERE m.id=%s AND i.id=%s AND i.kind='restart' "
            "AND i.status='done' "
            "AND COALESCE((i.payload -> 'lifecycle_release') = 'true'::jsonb, false) "
            "AND m.status='idling' AND m.runtime_owner IS NULL AND m.runtime_generation IS NULL",
            (agent_id, command_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"agent {agent_id} no longer has its executed restart")
