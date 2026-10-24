# meiva:note
# Close notices for busy persistent shells a unit closes at `meiva stop` or loses to a crash
# (issue #2044).
#
# `meiva stop` closes busy persistent-shell sessions in its `shell-sessions` phase, after the
# services are down and before the data plane stops. Each session it VERIFIED closed (its
# shell's exact identity gone, also when another session leaves the stop incomplete) gets one
# system inbound message for its owner agent, naming why it closed and known processes observed
# still running. The stop writes them itself over one short connection (`write_notices`) opened
# and closed inside that phase: the database is still up, and no pool or background task holds a
# client connection into the data plane's shutdown. A notice for a terminated owner is dropped
# without delivery (a closure notice must never resurrect a dead agent). The inbound row is
# pending for the owner's next claim (the canonical insert's best-effort wake finds no listener:
# the owner's agent host is already down).
#
# A gateway unit dials Postgres directly (its own pooler is about to stop); a runner-only unit
# has no local data plane and dials its configured URL, the gateway's database. A stop's notice
# that could not be written (the database became unreachable) is returned to the caller, which
# reports it on stderr; the stop neither retries nor stages.
#
# `write_notices` writes its whole batch in ONE transaction over that one connection: every
# idempotency claim and every inbound commits together or not at all, so a long sweep cannot
# leave a tail half-written. Delivery is idempotent per (machine, agent_id, session_id,
# shell-birth): the `api_idempotency` claim row and the inbound insert commit in one
# transaction, so a notice written twice is delivered once. The decisions (which keys are one
# notice, which owners are told) are `notice` and `delivery_plan`; this module reads the
# database and executes them.
#
# Crash sweeps log locally. They neither stage notices nor replay a previous start's journal.

from __future__ import annotations

import json
from collections.abc import Sequence

import psycopg
from psycopg import sql

from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.messages.inbound_provenance import InboundProvenance
from base.db import Database
from base.events.live.bus import EventBus
from base.log import logger
from base.native_process.ownership import OwnedProcess, shown_name
from base.sessions.pty import closure
from ops.cluster_status import AGENT_SHELL_RE
from ops.pty_close_notices.delivery_plan import plan_delivery
from ops.pty_close_notices.notice import (
    CRASH_REASON,
    STOP_REASON,
    ClosureNotice,
    claim_key,
    one_per_key,
)
from ops.rpc_schemas import OpStatus

__all__ = [
    "CRASH_REASON",
    "STOP_REASON",
    "ClosureNotice",
    "claim_all",
    "closure_notice",
    "content",
    "notices_for",
    "shell_birth",
    "write_notices",
]


# meiva:note
# The notice for one closed busy session; None when the name is not an agent shell.
#
# The caller guarantees the session was busy and that it closes it for `reason` at `closed_at`;
# `survivors` names, as (pid, command name), the session's known processes observed alive once
# its shell is verified gone. An agent-owned shell has the canonical `-agent-<id>-shell-<sid>`
# shape.
def closure_notice(
    *,
    machine: str,
    name: str,
    shell_pid: int,
    shell_birth: str,
    reason: str,
    closed_at: str,
    survivors: Sequence[tuple[int, str]] = (),
) -> ClosureNotice | None:
    match = AGENT_SHELL_RE.search(name)
    if match is None:
        return None
    return ClosureNotice(
        machine=machine,
        agent_id=int(match.group(1)),
        session_id=int(match.group(2)),
        name=name,
        shell_pid=shell_pid,
        shell_birth=shell_birth,
        reason=reason,
        closed_at=closed_at,
        survivors=tuple(survivors),
    )


# meiva:note
# The shell's birth as the dedup key spells it: one identity, one spelling, every path.
def shell_birth(shell: OwnedProcess) -> str:
    if shell.starttime is not None:
        return f"starttime:{shell.starttime}"
    return f"birth:{shell.birth!r}"


# meiva:note
# One notice per closed busy session that is an agent shell, from this machine.
def notices_for(
    closed: Sequence[closure.ClosedSession],
    *,
    reason: str,
    local_machine: str,
    closed_at: str,
) -> list[ClosureNotice]:
    built = (
        closure_notice(
            machine=local_machine,
            name=session.name,
            shell_pid=session.shell.pid,
            shell_birth=shell_birth(session.shell),
            reason=reason,
            closed_at=closed_at,
            survivors=session.left,
        )
        for session in closed
    )
    return [notice for notice in built if notice is not None]


# meiva:note
# The message the owner reads. A stop observed work, a sweep only has the recorded identities.
def content(notice: ClosureNotice) -> str:
    interruption = (
        "interrupting a running task. "
        if notice.reason == STOP_REASON
        else "probably interrupting a running task. "
    )
    text = (
        f"Shell session {notice.name!r} (id {notice.session_id}, agent {notice.agent_id}) "
        f"was closed by {notice.reason} on {notice.machine}, "
        + interruption
        + "Recreate the session if its work is still needed."
    )
    if notice.survivors:
        left = ", ".join(f"pid {pid} ({shown_name(name)})" for pid, name in notice.survivors)
        text += (
            f" Known processes observed after shell-session closure are still running: {left}. "
            "Inspect their ownership and current work before taking action; shell-session "
            "closure does not prove that all background or detached processes ended."
        )
    return text


# meiva:note
# Claim every notice's idempotency key in one statement; the keys this attempt owns.
#
# The whole batch goes in as one multi-row `ON CONFLICT (key) DO NOTHING RETURNING key`: the
# returned keys are the notices this attempt may deliver; a key an earlier attempt already
# claimed stays with it, so a re-sent notice is skipped, never delivered twice (issue #2044
# acceptance #4).
def claim_all(cur: psycopg.Cursor, batch: Sequence[ClosureNotice]) -> set[str]:
    group = sql.SQL("(%s, 'ops', 'closure-notice', %s, %s, now())")
    query = sql.SQL(
        "INSERT INTO api_idempotency (key, method, path, response_body, op_status, completed_at) "
        "VALUES {} ON CONFLICT (key) DO NOTHING RETURNING key"
    ).format(sql.SQL(", ").join([group] * len(batch)))
    params: list[object] = []
    for notice in batch:
        params.append(claim_key(notice))
        params.append(json.dumps(notice.as_dict(), default=str))
        params.append(OpStatus.COMPLETED)
    cur.execute(query, params)
    return {str(row[0]) for row in cur.fetchall()}


def _owner_statuses(cur: psycopg.Cursor, batch: Sequence[ClosureNotice]) -> dict[int, str | None]:
    cur.execute(
        "SELECT id, status FROM agents_meta WHERE id = ANY(%s)",
        (sorted({notice.agent_id for notice in batch}),),
    )
    return {int(row[0]): row[1] for row in cur.fetchall()}


# meiva:note
# Write the batch in the connection's one transaction; return (notice, inbound id) pairs.
#
# The transaction is declared writable first: a pooled session can default to read-only
# (`base.db.transaction.write_transaction`). Every claim goes in one statement, every owner's
# status is read in one, and each notice `plan_delivery` selects gets the canonical inbound
# insert (`insert_inbound_message_in_transaction`; its lineage event is None for a system-sourced
# chat). The single commit is the batch's durability point: everything before it rolls back
# together.
def _write_batch(
    conn: psycopg.Connection, notices: Sequence[ClosureNotice]
) -> list[tuple[ClosureNotice, int]]:
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ WRITE")
        plan = plan_delivery(
            notices,
            owned=frozenset(claim_all(cur, notices)),
            statuses=_owner_statuses(cur, notices),
        )
        delivered: list[tuple[ClosureNotice, int]] = []
        for notice in plan.deliver:
            inbound_id, _event = insert_inbound_message_in_transaction(
                cur,
                notice.agent_id,
                content(notice),
                source="system",
                payload={"closure": notice.as_dict()},
                provenance=InboundProvenance(source_verified_by=None, source_transport="ops"),
            )
            delivered.append((notice, inbound_id))
    conn.commit()
    for dropped in plan.dropped:
        logger.info(
            "[pty-close-notices] notice for agent {} (status {}) dropped — never resurrect",
            dropped.notice.agent_id,
            dropped.status,
        )
    for notice, _inbound_id in delivered:
        logger.info(
            "[pty-close-notices] wrote closure notice for agent {} session {}",
            notice.agent_id,
            notice.session_id,
        )
    return delivered


# meiva:note
# Write every notice in one transaction over one short connection; return those that failed.
#
# `direct` dials Postgres itself, bypassing the local pooler (a gateway unit, whose pooler stops
# right after the stop's shell-sessions phase); otherwise the configured URL is dialed (a
# runner-only unit: the gateway's database). No notice, no connection. The batch is one
# transaction, so a failed batch leaves nothing behind (no claim to skip the retry, no inbound
# that landed alone) and returns every notice with the error. The connection is closed before
# this returns. A notice written twice is delivered once: its key is spelled once per call, and a
# call that finds the key claimed skips it.
def write_notices(
    db: Database, bus: EventBus, notices: Sequence[ClosureNotice], *, direct: bool
) -> list[tuple[ClosureNotice, Exception]]:
    batch = one_per_key(notices)
    if not batch:
        return []
    try:
        conn = db.connect(direct=direct)
    except Exception as exc:
        return [(notice, exc) for notice in batch]
    try:
        with conn:
            delivered = _write_batch(conn, batch)
    except Exception as exc:
        return [(notice, exc) for notice in batch]
    for notice, inbound_id in delivered:
        # meiva:note
        # The canonical insert's fast path, after the rows are durable; in both flows that reach
        # here the owner's agent host is down, so the wake finds no listener: it costs a
        # best-effort publish either way.
        publish_inbound_wake(db, bus, notice.agent_id, str(inbound_id))
    return []
