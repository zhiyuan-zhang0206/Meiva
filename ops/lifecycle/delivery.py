"""Shared chat-inbound delivery for the gateway routers and the heartbeat service.

REST/SDK chat, MCP send_message, file uploads and completion digests
all do the same thing: persist one 'chat' inbound for an agent and announce it
for the live UI. `deliver_chat_inbound` is that one path, so each route keeps
only its own precondition (the `prepare` callback) instead of re-inlining the
INSERT + publish dance.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import NamedTuple

import psycopg
from psycopg_pool import ConnectionPool

from base import telemetry
from base.agents.inbox.chat_delivery import (
    ChatInboundCommittedError,
    ChatInboundReceipt,
    insert_chat_inbound_in_transaction,
    insert_chat_inbound_once,
    reconcile_chat_inbound,
)
from base.agents.inbox.insert import insert_inbound_message_in_transaction
from base.agents.inbox.wake import publish_inbound_wake
from base.agents.messages.inbound import PASSIVE_INBOUND_KEY, InboundKind
from base.agents.messages.inbound_provenance import InboundProvenance
from base.agents.messages.kwargs import NoteTag
from base.agents.messages.lifecycle_text import interrupt_note
from base.api_contracts.agent_contract import AgentNotFound, AgentStatus, Harness
from base.db import Database
from base.db.rows import query_one
from base.db.transaction import write_transaction
from base.events.live.announce import publish_agent_updated_sync
from base.events.live.bus import EventBus
from base.telemetry.audit_events import prepare_event_log, record_audit
from ops import lifecycle as ops
from ops.agents import get_agent_status
from ops.cluster.rpc import RpcClient


class ChatDelivery(NamedTuple):
    """Durable inbound receipt plus the status observed after auto-resurrect."""

    status: AgentStatus
    inbound_id: int | None


async def deliver_chat_inbound(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    agent_id: int,
    *,
    prepare: Callable[[psycopg.Connection], str | None],
    local_machine: str,
    rpc: RpcClient,
    source: str = "user",
    refresh_badge: bool = False,
    payload: dict[str, object] | None = None,
    client_message_id: str | None = None,
    provenance: InboundProvenance | None = None,
) -> ChatDelivery:
    """Deliver one 'chat' inbound to `agent_id` and announce it for the live UI.

    `prepare` runs inside the delivery transaction and returns the inbound text
    (or None to deliver no message, e.g. a report dismissed without a reply). It
    is where a route enforces its own precondition — marking a question answered,
    a report read — and may raise to abort before anything is written. When it
    returns text, the same transaction INSERTs it as the inbound so the agent's
    claim picks it up; when `refresh_badge`, the agent row is refreshed too (for
    endpoints whose unread counts change). `payload` is the optional JSONB
    sidecar written alongside — a multimodal message passes
    `{"content_blocks": [...]}` here while `prepare` returns the text part.
    After commit, reads delivery-time status and publishes InboundArrived.
    `client_message_id`, when present, identifies the logical message at the
    inbound INSERT itself: a same-id retry returns the existing inbound instead
    of duplicating it. Returns the delivery-time status and durable inbound id
    for the AgentMessageEnqueued response.

    Live publication completes in this caller. Known transport failures retain
    the bus's bounded best-effort policy; other post-commit failures propagate
    with the durable receipt. A failed notification never undoes the INSERT.
    """
    inbound: tuple[ChatInboundReceipt, str] | None = await asyncio.to_thread(
        _deliver_blocking,
        pool,
        db,
        bus,
        agent_id,
        prepare,
        source,
        payload,
        client_message_id,
        provenance,
    )
    if inbound is None:
        if refresh_badge:
            await asyncio.to_thread(publish_agent_updated_sync, bus, agent_id)
        status = await asyncio.to_thread(get_agent_status, db, agent_id)
        return ChatDelivery(status, None)
    receipt, content = inbound
    return await _finish_chat_delivery(
        db,
        bus,
        agent_id,
        receipt,
        content,
        source,
        client_message_id,
        refresh_badge=refresh_badge,
        local_machine=local_machine,
        rpc=rpc,
    )


async def _finish_chat_delivery(
    db: Database,
    bus: EventBus,
    agent_id: int,
    receipt: ChatInboundReceipt,
    content: str,
    source: str,
    client_message_id: str | None,
    *,
    refresh_badge: bool = False,
    local_machine: str,
    rpc: RpcClient,
) -> ChatDelivery:
    """Await separate wake, UI and resurrection effects after durable commit."""
    try:
        if refresh_badge:
            await asyncio.to_thread(publish_agent_updated_sync, bus, agent_id)
        if receipt.pending:
            if not receipt.inserted:
                # Same-key recovery heals a missed wake without another INSERT.
                await asyncio.to_thread(
                    publish_inbound_wake, db, bus, agent_id, str(receipt.inbound_id)
                )
            await ops.publish_inbound_arrived(
                bus, agent_id, receipt.inbound_id, "chat", source, content
            )
            status = await ops.resurrect_if_terminated(
                db,
                bus,
                agent_id,
                trigger_inbound_id=receipt.inbound_id,
                trigger_inbound_kind=InboundKind.CHAT,
                local_machine=local_machine,
                rpc=rpc,
            )
        else:
            # Claimed/done work is a receipt lookup, never a stale resurrection.
            status = await asyncio.to_thread(get_agent_status, db, agent_id)
    except Exception as exc:
        raise ChatInboundCommittedError(receipt, client_message_id, exc) from exc
    return ChatDelivery(status, receipt.inbound_id)


async def reconcile_chat_delivery(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    agent_id: int,
    *,
    client_message_id: str,
    content: str,
    source: str,
    payload: dict[str, object] | None,
    local_machine: str,
    rpc: RpcClient,
) -> ChatDelivery | None:
    """Resolve an uncertain send and heal any still-pending delivery tail.

    Receipt lookup is immutable and side-effect free. When the row is still
    pending, repeating the best-effort wake, live announcement, and exact-row
    resurrection closes the crash interval after COMMIT and before those
    effects. A claimed/done row returns its receipt without reviving stale work.
    """
    receipt = await asyncio.to_thread(
        _reconcile_blocking,
        pool,
        client_message_id,
        agent_id,
        content,
        source,
        payload,
    )
    if receipt is None:
        return None
    return await _finish_chat_delivery(
        db,
        bus,
        agent_id,
        receipt,
        content,
        source,
        client_message_id,
        local_machine=local_machine,
        rpc=rpc,
    )


def _deliver_blocking(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    agent_id: int,
    prepare: Callable[[psycopg.Connection], str | None],
    source: str,
    payload: dict[str, object] | None,
    client_message_id: str | None,
    provenance: InboundProvenance | None,
) -> tuple[ChatInboundReceipt, str] | None:
    """Sync delivery transaction — via to_thread: `prepare` runs inside it (it
    may execute its own DB statements), then the inbound INSERT. Returns
    (receipt, content) or None when prepare returned None."""
    with pool.connection() as conn:
        content = prepare(conn)
        if content is not None:
            receipt = insert_chat_inbound_once(
                conn,
                agent_id=agent_id,
                content=content,
                source=source,
                payload=payload,
                client_message_id=client_message_id,
                provenance=provenance,
                publish_wake=functools.partial(publish_inbound_wake, db, bus),
            )
            return (receipt, content)
    return None


def _reconcile_blocking(
    pool: ConnectionPool,
    client_message_id: str,
    agent_id: int,
    content: str,
    source: str,
    payload: dict[str, object] | None,
) -> ChatInboundReceipt | None:
    with pool.connection() as conn:
        return reconcile_chat_inbound(
            conn,
            client_message_id=client_message_id,
            agent_id=agent_id,
            content=content,
            source=source,
            payload=payload,
        )


@dataclass(frozen=True)
class _InterruptTarget:
    status: str
    harness: str


# meiva:note
# What one interrupt wrote, and whether a turn was there to interrupt.
class InterruptDelivery(NamedTuple):
    running: bool
    external: bool
    chat: ChatInboundReceipt | None


# meiva:note
# Write an interrupt's rows in one transaction, then announce them.
#
#     The target row is locked first, so "running" is read against agent admission and settlement.
#     A running target gets, in id order, a note saying its turn was interrupted by `source`, the
#     message as an ordinary chat from `source`, and (external harness only) the interrupt control
#     row its foreground watcher reacts to; the control row comes last, behind the input it
#     accompanies (a stop, which has no input, writes it first). A message to an agent with no running turn is a plain chat: no note, no control
#     row. Without a message (the web stop button) the note is passive: it never makes the agent
#     answer anything, and an idle agent is left alone. A native agent's passive note still publishes a
#     wake, so the claim that follows the cancelled turn takes it into the message list at once and
#     idles again without a model call; an external harness's is written to the timeline by its
#     foreground when the interruption is confirmed. The caller interrupts the turn only after this
#     commits, so the message is queued whether or not the interruption lands.
async def deliver_interrupt(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    agent_id: int,
    *,
    message: str | None,
    source: str,
    provenance: InboundProvenance | None,
    local_machine: str,
    rpc: RpcClient,
) -> InterruptDelivery:
    delivery = await asyncio.to_thread(
        _interrupt_blocking, pool, db, bus, agent_id, message, source, provenance
    )
    if delivery.chat is not None and message is not None:
        await _finish_chat_delivery(
            db,
            bus,
            agent_id,
            delivery.chat,
            message,
            source,
            None,
            local_machine=local_machine,
            rpc=rpc,
        )
    return delivery


# meiva:note
# The rows of one interrupt as they are written inside its transaction, and what must be announced
# after the commit: the events the writes recorded and the inbounds whose wake is to be published.
@dataclass
class _InterruptRows:
    conn: psycopg.Connection
    cur: psycopg.Cursor
    agent_id: int
    source: str
    wakes: list[int] = field(default_factory=list[int])
    events: list[telemetry.Event | None] = field(default_factory=list[telemetry.Event | None])

    def control(self) -> None:
        inbound_id, event = insert_inbound_message_in_transaction(
            self.cur, self.agent_id, "", self.source, kind=InboundKind.INTERRUPT.value
        )
        self.events.append(event)
        self.wakes.append(inbound_id)

    def note(self, *, with_message: bool, wake: bool) -> None:
        payload: dict[str, object] = {"note_tag": NoteTag.LIFECYCLE_INTERRUPT.value}
        if not with_message:
            payload[PASSIVE_INBOUND_KEY] = True
        inbound_id, event = insert_inbound_message_in_transaction(
            self.cur,
            self.agent_id,
            interrupt_note(self.source, with_message=with_message),
            self.source,
            kind=InboundKind.SYSTEM_NOTE.value,
            payload=payload,
        )
        self.events.append(event)
        if wake:
            self.wakes.append(inbound_id)

    def chat(self, message: str, provenance: InboundProvenance | None) -> ChatInboundReceipt:
        receipt, event = insert_chat_inbound_in_transaction(
            self.conn,
            agent_id=self.agent_id,
            content=message,
            source=self.source,
            payload=None,
            client_message_id=None,
            provenance=provenance,
        )
        self.events.append(event)
        self.wakes.append(receipt.inbound_id)
        return receipt

    def audit(self, chat: ChatInboundReceipt | None) -> None:
        self.events.append(
            record_audit(
                self.conn,
                prepare_event_log(
                    event_type="interrupt",
                    agent_id=self.agent_id,
                    source=self.source,
                    target_agent_id=_agent_of(self.source),
                    payload={"inbound_id": chat.inbound_id} if chat is not None else {},
                ),
            )
        )


# meiva:note
# The control row goes last behind a message, so the message is delivered first. A stop has no message
# and its passive note is never handed to the harness on its own, so the control row goes first: behind
# the note it would wait for a delivery that never comes.
def _write_interrupt_rows(
    rows: _InterruptRows,
    *,
    running: bool,
    external: bool,
    message: str | None,
    provenance: InboundProvenance | None,
) -> ChatInboundReceipt | None:
    chat: ChatInboundReceipt | None = None
    control_first = running and external and message is None
    if control_first:
        rows.control()
    if running:
        rows.note(with_message=message is not None, wake=message is not None or not external)
    if message is not None:
        chat = rows.chat(message, provenance)
    if running:
        if external and not control_first:
            rows.control()
        rows.audit(chat)
    return chat


def _interrupt_blocking(
    pool: ConnectionPool,
    db: Database,
    bus: EventBus,
    agent_id: int,
    message: str | None,
    source: str,
    provenance: InboundProvenance | None,
) -> InterruptDelivery:
    with write_transaction(pool) as conn:
        target = query_one(
            conn,
            _InterruptTarget,
            "SELECT status, harness FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        if target is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        running = AgentStatus(target.status) is AgentStatus.RUNNING
        external = Harness(target.harness) is not Harness.MEIVA
        with conn.transaction(), conn.cursor() as cur:
            rows = _InterruptRows(conn, cur, agent_id, source)
            chat = _write_interrupt_rows(
                rows, running=running, external=external, message=message, provenance=provenance
            )
    for event in rows.events:
        if event is not None:
            telemetry.emit_prepared(event)
    for inbound_id in rows.wakes:
        publish_inbound_wake(db, bus, agent_id, str(inbound_id))
    return InterruptDelivery(running, external, chat)


def _agent_of(source: str) -> int | None:
    return int(source.removeprefix("agent:")) if source.startswith("agent:") else None
