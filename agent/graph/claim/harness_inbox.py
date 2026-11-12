# meiva:note
# The inbox side of external-harness delivery: reserve the head batch, settle it, give it back. One
# state expresses "handed to the harness, not settled": `status='claimed'`, for every deliverable kind.
# A row leaves it only through the harness's own facts: in the context (done, with its timeline entries
# in the same transaction), or refused or lost (back to `pending` at its original queue position).
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import LiteralString, Protocol

from langchain_core.messages import BaseMessage
from psycopg import sql

from agent.db import CLAIM_ACQUIRE_TIMEOUT_CAP_S, ClaimedInbound
from agent.graph.claim._dispatch import prepare_steered_inbound
from agent.graph.claim.harness_activity import TurnSink
from agent.graph.claim.harness_foreground import Handed, Reconciled, plan_reconciliation
from agent.graph.claim.harness_render import render_message
from agent.graph.claim.timeline import append_turn_timeline
from agent.graph.hooks.user_delivery import delivery_end_reminder
from agent.messages.reply_visibility import reply_visibility_note
from agent.ownership.inbound import lock_inbound_owner
from base.agents.context import MeivaContext
from base.agents.history.harness_messages import aappend_harness_timeline_items
from base.agents.history.timeline import build_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import PASSIVE_INBOUND_KEY, InboundKind, not_passive_inbound
from base.agents.messages.kwargs import NoteTag
from base.db.rows import IdRow, aquery_all
from base.db.transaction import async_write_transaction
from base.events.live.projection import InboundCommitted, TimelineSnapshot
from base.lm.harness import ReconciliationUnavailableError
from base.lm.harness.engine import TurnEngine
from base.log import logger

DELIVERABLE_INBOUND_KINDS = (
    InboundKind.CHAT.value,
    InboundKind.SYSTEM_NOTE.value,
    InboundKind.HEARTBEAT.value,
)

# meiva:note
# The payload field that names the one harness input a claimed row rides in, written when a batch of
# several rows is handed over as one input, so a later run can rebuild the group before it asks the
# harness's record about the input's key.
# meiva:link agent/graph/docs/harness-foreground.md
INPUT_KEY_FIELD = "harness_input_key"

_COLUMNS: LiteralString = "id,agent_id,content,kind,source,payload,created_at,claimed_at"

# meiva:note
# The contiguous FIFO batch of pending deliverable rows at the head of the inbox. Any other pending kind
# is a barrier for the rows behind it, by the same (created_at, id) order the claim uses to take control
# rows, so each side waits only for what is in front of it: an interrupt queued behind a message lets the message
# go and is claimed once it is out of the way. Nothing is handed over while a restart or terminate is pending
# or claimed: the claim takes it first and alone. A batch of passive notes alone is not handed over: a
# delivery would start a turn of the harness's own for notes that asked for none, so they wait and go
# along with the next input that is not passive.
# meiva:link agent/graph/docs/harness-turn.md
_HEAD_ROWS: LiteralString = (
    "SELECT m.id, m.payload FROM inbound_messages m "
    "WHERE m.agent_id=%(agent)s AND m.status='pending' AND m.kind = ANY(%(kinds)s) "
    "  AND NOT EXISTS ("
    "    SELECT 1 FROM inbound_messages b WHERE b.agent_id=%(agent)s AND b.status='pending' "
    "      AND NOT (b.kind = ANY(%(kinds)s)) AND (b.created_at, b.id) <= (m.created_at, m.id)"
    "  ) "
    "  AND NOT EXISTS ("
    "    SELECT 1 FROM inbound_messages control WHERE control.agent_id=%(agent)s "
    "    AND control.status IN ('pending','claimed') "
    "    AND control.kind IN ('restart','terminate')"
    "  )"
)

# meiva:note
# The head batch, or nothing when every row in it is passive. Composed from literal fragments only.
_HEAD_IDS: LiteralString = (
    f"SELECT h.id FROM ({_HEAD_ROWS}) h "  # noqa: S608
    f"WHERE EXISTS (SELECT 1 FROM ({_HEAD_ROWS}) x WHERE {not_passive_inbound('x')})"
)


# meiva:note
# Settle the interrupt requests still pending when an invocation starts. The claim takes an interrupt that
# stands in front of the inbox before the invocation begins, so one left behind a note or a message was
# aimed at a turn that has since ended by itself. Left pending it would fire at the first turn of this
# invocation, the one that is answering the message, and cut it short.
async def settle_stale_interrupts(ctx: MeivaContext, agent_id: int) -> None:
    pool = ctx.ops_pool
    if pool is None:
        return
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        _ = await conn.execute(
            "UPDATE inbound_messages SET status='done' "
            "WHERE agent_id=%s AND kind=%s AND status='pending'",
            (agent_id, InboundKind.INTERRUPT.value),
        )


# meiva:note
# The payload flag of a passive note whose timeline entry was written ahead of its delivery. The row is
# still pending and rides in the harness's next input as text, but its entry is not written a second time.
TIMELINE_WRITTEN_KEY = "timeline_written"

# meiva:note
# The passive interrupt notes of a stop that are not yet on the timeline, flagged as they are taken.
_UNWRITTEN_INTERRUPT_NOTES: LiteralString = (
    "UPDATE inbound_messages SET payload = payload || jsonb_build_object(%(written)s::text, true) "  # noqa: S608
    "WHERE agent_id=%(agent)s AND status='pending' AND kind=%(kind)s "
    "  AND payload->>'note_tag'=%(tag)s AND payload->'passive'='true'::jsonb "
    "  AND NOT COALESCE(payload->%(written)s::text='true'::jsonb, false) "
    f"RETURNING {_COLUMNS}"
)


# meiva:note
# A confirmed stop is on the timeline at once. Its passive note is no input of the harness's (a
# delivery would start a turn), so it waits in the inbox for the next real input; here its entry is
# written and the row flagged in one transaction, and `timeline_items_of` drops the entry when the row is
# finally delivered, so the note appears once: a crash before this transaction leaves the row to be
# delivered with its entry, one after it leaves the entry already written. A row an input already
# reserved is not touched: that input writes the entry when it settles.
async def write_interrupt_notes(ctx: MeivaContext, agent_id: int) -> list[TimelineItem]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        cursor = await conn.execute(
            sql.SQL(_UNWRITTEN_INTERRUPT_NOTES),
            {
                "agent": agent_id,
                "kind": InboundKind.SYSTEM_NOTE.value,
                "tag": NoteTag.LIFECYCLE_INTERRUPT.value,
                "written": TIMELINE_WRITTEN_KEY,
            },
        )
        rows = sorted(
            (ClaimedInbound.from_row(row) for row in await cursor.fetchall()),
            key=lambda row: (row.created_at, row.id),
        )
        messages = [msg for row in rows for msg in prepare_steered_inbound(ctx, row)]
        items, _ = build_timeline_items(messages, inputs=ctx.timestamp_policy())
        return await aappend_harness_timeline_items(conn, agent_id, ctx.harness.value, items)


# meiva:note
# A stop's passive note: no input of the harness's, it rides in the next input's text.
def _is_interrupt_note(row: ClaimedInbound) -> bool:
    return (
        row.kind == InboundKind.SYSTEM_NOTE.value
        and row.payload is not None
        and row.payload.get("note_tag") == NoteTag.LIFECYCLE_INTERRUPT.value
    )


# meiva:note
# The timeline entries of a row about to be handed to the harness: none for a note already written, and none for
# a stop's passive note when the timeline is projected from the CLI's record (the record's own interrupt marker is
# the only one; the note still reaches the CLI with the next input).
def timeline_items_of(
    row: ClaimedInbound, messages: Sequence[BaseMessage], ctx: MeivaContext
) -> list[TimelineItem]:
    if row.payload is not None and row.payload.get(TIMELINE_WRITTEN_KEY) is True:
        return []
    if ctx.harness_timeline is not None and _is_interrupt_note(row):
        return []
    items, _ = build_timeline_items(messages, inputs=ctx.timestamp_policy())
    return items


async def peek_head_batch(ctx: MeivaContext, agent_id: int) -> list[ClaimedInbound]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    async with pool.connection() as conn:
        cursor = await conn.execute(
            sql.SQL(
                "SELECT {columns} FROM inbound_messages WHERE id IN ({head}) "
                "ORDER BY created_at ASC, id ASC"
            ).format(columns=sql.SQL(_COLUMNS), head=sql.SQL(_HEAD_IDS)),
            {"agent": agent_id, "kinds": list(DELIVERABLE_INBOUND_KINDS)},
        )
        return [ClaimedInbound.from_row(row) for row in await cursor.fetchall()]


# meiva:note
# The deliverable rows an earlier invocation or process handed to the harness and left claimed: this run
# did not hand them, so nothing settles them unless they are reconciled against the harness's own
# record (`ForegroundRun` does, before it delivers anything).
# meiva:link agent/graph/docs/harness-foreground.md
async def claimed_deliverable(
    ctx: MeivaContext, agent_id: int, *, include_deferred: bool
) -> list[ClaimedInbound]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    async with pool.connection() as conn:
        cursor = await conn.execute(
            sql.SQL(
                "SELECT {columns} FROM inbound_messages "
                "WHERE agent_id=%(agent)s AND status='claimed' AND kind = ANY(%(kinds)s) "
                "AND (%(deferred)s OR COALESCE(payload->>'harness_reconcile_deferred', '') = '') "
                "ORDER BY created_at ASC, id ASC"
            ).format(columns=sql.SQL(_COLUMNS)),
            {
                "agent": agent_id,
                "kinds": list(DELIVERABLE_INBOUND_KINDS),
                "deferred": include_deferred,
            },
        )
        return [ClaimedInbound.from_row(row) for row in await cursor.fetchall()]


async def reserve_head_batch(ctx: MeivaContext, agent_id: int) -> list[ClaimedInbound]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    timeout = min(CLAIM_ACQUIRE_TIMEOUT_CAP_S, pool.timeout)
    async with async_write_transaction(pool, timeout=timeout) as conn:
        _ = await conn.execute("SELECT set_config('lock_timeout', %s, true)", (f"{timeout:g}s",))
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        cursor = await conn.execute(
            sql.SQL(
                "UPDATE inbound_messages SET status='claimed', claimed_at=clock_timestamp() "
                "WHERE id IN ({head}) RETURNING {columns}"
            ).format(columns=sql.SQL(_COLUMNS), head=sql.SQL(_HEAD_IDS)),
            {"agent": agent_id, "kinds": list(DELIVERABLE_INBOUND_KINDS)},
        )
        rows = await cursor.fetchall()
        if rows:
            _ = await conn.execute(
                "UPDATE agents_meta SET wake_suppressed_until=NULL, wake_suppress_reason=NULL "
                "WHERE id=%s AND wake_suppressed_until IS NOT NULL",
                (agent_id,),
            )
    claimed = [ClaimedInbound.from_row(row) for row in rows]
    claimed.sort(key=lambda item: (item.created_at, item.id))
    return claimed


# meiva:note
# ACK 2: the rows become done and their timeline entries are written in one transaction, so the pending
# strip and the timeline never disagree. The caller publishes only after the commit.
async def commit_handed(
    ctx: MeivaContext, agent_id: int, handed: Sequence[Handed]
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    if pool is None:
        return []
    ids = [i for h in handed for i in h.row_ids]
    items = [item for h in handed for item in h.items]
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        if ids:
            rows = await aquery_all(
                conn,
                IdRow,
                "UPDATE inbound_messages SET status='done' "
                "WHERE agent_id=%s AND id=ANY(%s) AND kind=ANY(%s) AND status='claimed' "
                "RETURNING id",
                (agent_id, ids, list(DELIVERABLE_INBOUND_KINDS)),
            )
            if len(rows) != len(ids):
                raise RuntimeError(f"harness delivery claim changed for agent {agent_id}: {ids}")
        return await aappend_harness_timeline_items(conn, agent_id, ctx.harness.value, items)


# meiva:note
# The harness's own record puts the input in the model's context, so it settles like an
# acknowledgement. The row may already have been settled by the run that was failing (a write that
# finished as the failure struck), so only a row still claimed gets its timeline entries and nothing is
# written twice.
async def commit_reconciled(
    ctx: MeivaContext, agent_id: int, handed: Sequence[Handed]
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    if pool is None or not handed:
        return []
    ids = [i for h in handed for i in h.row_ids]
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        rows = (
            await aquery_all(
                conn,
                IdRow,
                "UPDATE inbound_messages SET status='done' "
                "WHERE agent_id=%s AND id=ANY(%s) AND kind=ANY(%s) AND status='claimed' "
                "RETURNING id",
                (agent_id, ids, list(DELIVERABLE_INBOUND_KINDS)),
            )
            if ids
            else []
        )
        settled = {row.id for row in rows}
        items = [
            item
            for h in handed
            if not h.row_ids or settled.intersection(h.row_ids)
            for item in h.items
        ]
        return await aappend_harness_timeline_items(conn, agent_id, ctx.harness.value, items)


# meiva:note
# A refused or lost input goes back to the queue: `pending` with no claim time, so it sorts at its
# original position and the next reservation hands it over again, ahead of anything newer.
async def requeue_handed(ctx: MeivaContext, agent_id: int, handed: Sequence[Handed]) -> None:
    pool = ctx.ops_pool
    ids = [i for h in handed for i in h.row_ids]
    if pool is None or not ids:
        return
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        _ = await conn.execute(
            "UPDATE inbound_messages SET status='pending', claimed_at=NULL, "
            "payload = payload - %s "
            "WHERE agent_id=%s AND id=ANY(%s) AND kind=ANY(%s) AND status='claimed'",
            (INPUT_KEY_FIELD, agent_id, ids, list(DELIVERABLE_INBOUND_KINDS)),
        )


async def mark_input_group(
    ctx: MeivaContext, agent_id: int, row_ids: Sequence[int], key: str
) -> None:
    pool = ctx.ops_pool
    if pool is None:
        return
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        _ = await conn.execute(
            "UPDATE inbound_messages SET payload = COALESCE(payload, '{}'::jsonb) "
            "|| jsonb_build_object(%s::text, %s::text) "
            "WHERE agent_id=%s AND id=ANY(%s) AND status='claimed'",
            (INPUT_KEY_FIELD, key, agent_id, list(row_ids)),
        )


# meiva:note
# Inputs in flight whose fate cannot be established: the harness's durable record could not be asked
# (or its session could not be retired first), so they are consumed and marked rather than sent a
# second time. When the record can be asked, `reconcile_handed` decides each input instead and
# this is only the fallback.
# meiva:link agent/graph/docs/harness-turn.md
async def retire_unconfirmed(
    ctx: MeivaContext, agent_id: int, handed: Sequence[Handed]
) -> list[TimelineItem]:
    pool = ctx.ops_pool
    ids = [i for h in handed for i in h.row_ids]
    if pool is None or not ids:
        return []
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        rows = await aquery_all(
            conn,
            IdRow,
            "UPDATE inbound_messages SET status='done' "
            "WHERE agent_id=%s AND id=ANY(%s) AND kind=ANY(%s) AND status='claimed' RETURNING id",
            (agent_id, ids, list(DELIVERABLE_INBOUND_KINDS)),
        )
        if not rows:
            return []
        items = unconfirmed_input_items(
            sorted(row.id for row in rows), created_at=ctx.require_clock().now().isoformat()
        )
        return await aappend_harness_timeline_items(conn, agent_id, ctx.harness.value, items)


def unconfirmed_input_items(inbound_ids: Sequence[int], *, created_at: str) -> list[TimelineItem]:
    return [
        TimelineItem(
            item_id="0.0",
            kind="system_marker",
            source=NoteTag.TURN_LOST.value,
            payload=f"Delivery of message {inbound_id} to the harness could not be "
            "confirmed. It was retired from the queue to avoid sending it twice.",
            created_at=created_at,
            inbound_id=inbound_id,
        )
        for inbound_id in inbound_ids
    ]


# meiva:note
# Ask the harness's own durable record what became of inputs that were handed over and never
# acknowledged: the effect half of reconciliation (the decision is `plan_reconciliation`). An answer that
# cannot be had (no session record, a record that is gone or unreadable) is a failure, never a guess: the
# caller then falls back to the uncertainty marker, because only a record that exists and lacks the input
# may let it be delivered again.
# meiva:link agent/graph/docs/harness-foreground.md
async def reconcile_handed(
    bridge: TurnEngine, agent_id: int, handed: Sequence[Handed], *, where: str
) -> Reconciled | None:
    keys = [h.key for h in handed]
    try:
        plan = plan_reconciliation(handed, await bridge.reconcile(keys))
    except ReconciliationUnavailableError as exc:
        logger.warning(
            "the harness has no durable record to reconcile its inputs against",
            event="harness_reconcile_failed",
            agent_id=agent_id,
            keys=keys,
            where=where,
            error=str(exc),
        )
        return None
    for outcome, entries in (
        ("in_context", plan.commit),
        ("not_found", plan.requeue),
        ("persisted_undelivered", plan.keep),
        ("unknown", plan.unknown),
    ):
        for entry in entries:
            logger.info(
                "reconciled an input handed to the harness",
                event="harness_input_reconciled",
                agent_id=agent_id,
                key=entry.key,
                outcome=outcome,
                where=where,
            )
    return plan


# meiva:note
# Consume the inputs with the uncertainty marker and say so in the timeline; returns the inbox rows it
# marked, which are the only ones a "turn lost" note may name.
async def mark_unconfirmed(
    ctx: MeivaContext, agent_id: int, projector: TurnSink, handed: Sequence[Handed]
) -> list[int]:
    items = await projector.append_inbound(partial(retire_unconfirmed, ctx, agent_id, handed))
    publish_inbound(ctx, agent_id, items)
    return [item.inbound_id for item in items if item.inbound_id is not None]


# meiva:note
# Write what a reconciliation decided: inputs in the context settle like an acknowledgement, absent ones go
# back to the queue (the caller wakes the delivery loop), inputs the record cannot tell about are consumed
# with the marker. Inputs the harness stored itself are left alone. Returns the rows marked.
async def apply_reconciled(
    ctx: MeivaContext, agent_id: int, projector: TurnSink, plan: Reconciled
) -> list[int]:
    for entry in plan.commit:
        items = await projector.append_inbound(
            partial(commit_reconciled, ctx, agent_id, [entry]), human_input=entry.human
        )
        publish_inbound(ctx, agent_id, items)
    if plan.requeue:
        await requeue_handed(ctx, agent_id, plan.requeue)
    if plan.unknown:
        return await mark_unconfirmed(ctx, agent_id, projector, plan.unknown)
    return []


# meiva:note
# A row the harness stored and still has not delivered after one more reconciliation: it stays claimed
# and is marked, so neither the claim nor the pending scan wakes the agent for it again. The next run that
# starts for another reason reconciles it once more.
async def defer_reconciliation(ctx: MeivaContext, agent_id: int, handed: Sequence[Handed]) -> None:
    pool = ctx.ops_pool
    ids = [i for h in handed for i in h.row_ids]
    if pool is None or not ids:
        return
    async with async_write_transaction(pool) as conn:
        await lock_inbound_owner(conn, agent_id, incarnation=ctx.original_incarnation)
        _ = await conn.execute(
            "UPDATE inbound_messages SET payload = COALESCE(payload, '{}'::jsonb) "
            "|| jsonb_build_object('harness_reconcile_deferred', true) "
            "WHERE agent_id=%s AND id=ANY(%s) AND status='claimed'",
            (agent_id, ids),
        )


# meiva:note
# A restart or resurrect note is a pending row like any other, so a row returned to the queue by a
# reconciliation sorts ahead of it by its original position. The agent has to learn that it was
# restarted before it sees the messages that are delivered again, so within a batch the lifecycle
# notes go first (stable: everything else keeps its queue order).
def recovery_notes_first(rows: Sequence[ClaimedInbound]) -> list[ClaimedInbound]:
    def is_note(row: ClaimedInbound) -> bool:
        return row.kind == InboundKind.SYSTEM_NOTE.value and bool(
            row.payload
            and row.payload.get("note_tag")
            in (
                NoteTag.LIFECYCLE_RESTART.value,
                NoteTag.LIFECYCLE_RESURRECT.value,
                NoteTag.LIFECYCLE_INTERRUPT.value,
            )
        )

    return [*(r for r in rows if is_note(r)), *(r for r in rows if not is_note(r))]


def is_passive(row: ClaimedInbound) -> bool:
    return bool(row.payload and row.payload.get(PASSIVE_INBOUND_KEY) is True)


def is_deferred(row: ClaimedInbound) -> bool:
    return bool(row.payload and row.payload.get("harness_reconcile_deferred") is True)


class Emits(Protocol):
    def emit(self, payload: str) -> None: ...


def publish_inbound(ctx: MeivaContext, agent_id: int, items: list[TimelineItem]) -> None:
    publisher = ctx.event_publisher
    if publisher is None:
        return
    publish_items(publisher, agent_id, items)


# meiva:note
# The live view's snapshot of written timeline items, and the inbound-committed event of each inbound item, on a
# given publisher: the invocation's own (`publish_inbound`), or the runtime-scoped one the transcript timeline
# writer publishes on, which does not stop when an invocation ends.
def publish_items(publisher: Emits, agent_id: int, items: list[TimelineItem]) -> None:
    if not items:
        return
    count = int(items[-1].item_id.split(".")[0].removeprefix("h")) + 1
    publisher.emit(
        TimelineSnapshot(
            agent_id=agent_id, items=[item.model_dump() for item in items], msg_count=count
        ).model_dump_json()
    )
    for item in items:
        if item.kind == "inbound_chat" and item.inbound_id is not None:
            publisher.emit(
                InboundCommitted(agent_id=agent_id, inbound_id=item.inbound_id).model_dump_json()
            )


# meiva:note
# The correction input for a reply that broke the delivery contract, or None.
#
# The correction is a synthetic input like any other: its note reaches the timeline when the harness
# reports it in context. Asking twice would loop, so a second violation is only recorded.
async def harness_reply_correction(
    ctx: MeivaContext,
    agent_id: int,
    baseline: int | None,
    *,
    invalid: bool,
    key: str,
    correcting: bool,
) -> tuple[Handed, str] | None:
    note = await delivery_end_reminder(ctx, agent_id, baseline, correcting=correcting)
    if not invalid and note is None:
        return None
    note = note or reply_visibility_note(ctx.require_clock().now())
    if correcting:
        await append_turn_timeline(ctx, agent_id, [note])
        return None
    items, _ = build_timeline_items([note], inputs=ctx.timestamp_policy())
    entry = Handed(f"{key}:visibility-correction", None, tuple(items))
    return entry, render_message(note)
