from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import PlannedItem, ReplyPolicy, WritePlan, plan_write
from agent.ownership.inbound import RuntimeOwnershipLostError, lock_inbound_owner
from base.agents.history.harness_messages import (
    StoredPosition,
    aappend_keyed_timeline_items,
    aappend_transcript_rows,
    aget_harness_call_item_id,
    aload_transcript_positions,
    aload_transcript_rows,
    aset_transcript_position,
)
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import InboundKind
from base.clock import Clock
from base.db.transaction import async_write_transaction
from base.host.boundary.json_values import is_object_dict
from base.lm.harness.session.transcript.drafts import Projection, SentInput
from base.lm.harness.session.transcript.ports import EntryBatch, RecordReader, StoredRow
from base.lm.harness.session.transcript.projection import Projector
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation, StaleIncarnationError

# meiva:note
# The session-scoped writer of an external agent's timeline. It lives as long as the session, not as long as a
# graph invocation, and it does one thing in a loop: when woken (a stream frame, a `session_store` callback, a
# poll) it reads every record after its stored position, projects what it read, and commits the result in one
# transaction: the whitelisted rows of the read (appended once), keyed timeline items appended in the order
# read, the inbound rows the entries prove delivered set `done`, and the new positions. A commit that fails is
# retried with the same plan and nothing is read in between, so entries are never lost to the projector's
# memory of having seen them; a stale incarnation ends the writer. A restart rebuilds the projector's state
# (pairing, delivery) from the stored rows after each record's rebuild ordinal, in bounded batches and without
# touching the CLI's file, and projects only what lies past the stored position. What it commits is handed to
# `after_commit` once, for the caller to publish, capture for delivery, price and wake the graph with.
# meiva:link agent/harness_timeline/README.md

POLL_SECONDS = 3.0
RETRY_SECONDS = 2.0
FAULT_AFTER_SECONDS = 60.0
PROJECTION_VERSION = 1
REBUILD_BATCH_ROWS = 500
_SETTLE_STATUSES = ("claimed", "pending")


class TurnEndWait(StrEnum):
    CLOSED = "closed"
    QUIET = "quiet"
    CEILING = "ceiling"


@dataclass(frozen=True, slots=True)
class Committed:
    plan: WritePlan
    written: tuple[TimelineItem, ...]


@dataclass(slots=True)
class _Sent:
    items: dict[str, TimelineItem] = field(default_factory=dict[str, TimelineItem])
    inbound_ids: dict[str, tuple[int, ...]] = field(default_factory=dict[str, tuple[int, ...]])


@dataclass(frozen=True, slots=True)
class _Pending:
    plan: WritePlan
    batches: list[EntryBatch]


def _mapping(row: object) -> Mapping[str, object]:
    if not is_object_dict(row):
        raise TypeError("a stored transcript row is not an object")
    return row


class HarnessTimelineWriter:
    def __init__(
        self,
        *,
        pool: AsyncConnectionPool,
        agent_id: int,
        harness: str,
        session_id: str,
        incarnation: RuntimeIncarnation | None,
        reader: RecordReader,
        policy: ReplyPolicy,
        render_tool: Callable[[str, Mapping[str, object]], str],
        after_commit: Callable[[Committed], Awaitable[None]],
        cli_version: Callable[[], str | None],
        clock: Clock,
    ) -> None:
        self._clock = clock
        self._pool = pool
        self._agent_id = agent_id
        self._harness = harness
        self._session_id = session_id
        self._incarnation = incarnation
        self._reader = reader
        self._policy = policy
        self._render_tool = render_tool
        self._after_commit = after_commit
        self._cli_version = cli_version
        self._projector = Projector()
        self._sent = _Sent()
        self._positions: dict[str, StoredPosition] = {}
        self._last_ordinal: dict[str, int] = {}
        self._pending: _Pending | None = None
        self._non_human: set[str] = set()
        self._started = False
        self._wake = asyncio.Event()
        self._closed_turns = 0
        self._stopping = False
        self._fault: Exception | None = None

    @property
    def closed_turns(self) -> int:
        return self._closed_turns

    def register_sent(
        self, sent: SentInput, *, items: Mapping[str, TimelineItem], inbound_ids: tuple[int, ...]
    ) -> None:
        self._projector.register(sent)
        self._sent.items.update(items)
        self._sent.inbound_ids[sent.key] = inbound_ids

    def undelivered(self) -> tuple[SentInput, ...]:
        return self._projector.undelivered()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def read_now(self) -> bool:
        if not self._started:
            await self._start_up()
        if self._pending is not None:
            return await self._commit_pending()
        batches = await self._read_all()
        if not batches:
            return False
        projection = self._project(batches)
        plan = plan_write(
            projection,
            sent_items=self._sent.items,
            inbound_ids=self._sent.inbound_ids,
            non_human_turns=frozenset(self._non_human),
            policy=self._policy,
            render_tool=self._render_tool,
        )
        self._note_turns(plan)
        self._pending = _Pending(plan, batches)
        return await self._commit_pending()

    async def wait_for_turn_end(
        self, closed_before: int, *, quiet_seconds: float, ceiling_seconds: float
    ) -> TurnEndWait:
        deadline = self._clock.monotonic() + ceiling_seconds
        last_activity = self._clock.monotonic()
        while True:
            if await self.read_now():
                last_activity = self._clock.monotonic()
            if self._closed_turns > closed_before:
                return TurnEndWait.CLOSED
            now = self._clock.monotonic()
            if now >= deadline:
                return TurnEndWait.CEILING
            if now - last_activity >= quiet_seconds:
                return TurnEndWait.QUIET
            await asyncio.sleep(min(0.1, quiet_seconds))

    @property
    def fault(self) -> Exception | None:
        return self._fault

    async def run(self) -> None:
        failing_since: float | None = None
        while not self._stopping:
            try:
                _ = await self.read_now()
                failing_since = None
            except (StaleIncarnationError, RuntimeOwnershipLostError):
                return
            except Exception as error:
                failing_since = (
                    failing_since if failing_since is not None else self._clock.monotonic()
                )
                logger.opt(exception=True).warning(
                    "the harness timeline writer could not read or commit",
                    event="harness_timeline_writer_retry",
                    agent_id=self._agent_id,
                )
                if self._clock.monotonic() - failing_since >= FAULT_AFTER_SECONDS:
                    self._fault = error
                    return
                await asyncio.sleep(RETRY_SECONDS)
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
            self._wake.clear()
        if self._started and self._fault is None:
            _ = await self.read_now()

    async def _start_up(self) -> None:
        async with self._pool.connection() as conn:
            self._positions = await aload_transcript_positions(
                conn, self._agent_id, self._session_id
            )
        for record, stored in self._positions.items():
            await self._rebuild(record, stored.rebuild_ordinal)
        self._started = True

    async def _rebuild(self, record: str, after_ordinal: int) -> None:
        cursor = after_ordinal
        while True:
            async with self._pool.connection() as conn:
                loaded = await aload_transcript_rows(
                    conn,
                    self._agent_id,
                    self._session_id,
                    record,
                    after_ordinal=cursor,
                    limit=REBUILD_BATCH_ROWS,
                )
            if not loaded:
                return
            rows = [StoredRow(record, ordinal, _mapping(row)) for ordinal, row in loaded]
            _ = self._projector.feed(self._reader.parse_stored(rows))
            cursor = rows[-1].ordinal
            self._last_ordinal[record] = cursor
            if len(loaded) < REBUILD_BATCH_ROWS:
                return

    async def _read_all(self) -> list[EntryBatch]:
        batches: list[EntryBatch] = []
        for record in await self._reader.records():
            stored = self._positions.get(record)
            batch = await self._reader.read_after(record, stored.position if stored else None)
            if batch is not None:
                batches.append(batch)
        return batches

    def _project(self, batches: list[EntryBatch]) -> Projection:
        return self._projector.feed([entry for batch in batches for entry in batch.entries])

    def _note_turns(self, plan: WritePlan) -> None:
        for turn in plan.observed:
            if turn.origin.value != "input":
                self._non_human.add(turn.turn)
            else:
                self._non_human.discard(turn.turn)

    async def _commit_pending(self) -> bool:
        pending = self._pending
        if pending is None:
            return False
        plan = pending.plan
        positions = self._next_positions(pending)
        written = await self._commit(plan, pending.batches, positions)
        self._positions.update({stored.record: stored for stored in positions})
        for batch in pending.batches:
            if batch.rows:
                self._last_ordinal[batch.record] = batch.rows[-1].ordinal
        self._pending = None
        self._closed_turns += len(plan.closed)
        await self._after_commit(Committed(plan, tuple(written)))
        return True

    def _next_positions(self, pending: _Pending) -> list[StoredPosition]:
        quiet = bool(pending.plan.closed) and self._quiet()
        result: list[StoredPosition] = []
        for batch in pending.batches:
            previous = self._positions.get(batch.record)
            last = (
                batch.rows[-1].ordinal if batch.rows else self._last_ordinal.get(batch.record, -1)
            )
            rebuild = last if quiet else (previous.rebuild_ordinal if previous else -1)
            result.append(StoredPosition(batch.record, batch.position, rebuild))
        return result

    def _quiet(self) -> bool:
        return not (
            self._projector.unanswered_calls()
            or self._projector.unresolved_results()
            or self._projector.unlinked_records()
        )

    async def _commit(
        self, plan: WritePlan, batches: list[EntryBatch], positions: list[StoredPosition]
    ) -> list[TimelineItem]:
        async with async_write_transaction(self._pool) as conn:
            await lock_inbound_owner(conn, self._agent_id, incarnation=self._incarnation)
            _ = await aappend_transcript_rows(
                conn,
                self._agent_id,
                self._session_id,
                [(row.record, row.ordinal, row.row) for batch in batches for row in batch.rows],
            )
            written = await self._append(conn, plan.items)
            for settlement in plan.settlements:
                if settlement.inbound_ids:
                    _ = await conn.execute(
                        "UPDATE inbound_messages SET status='done' "
                        "WHERE agent_id=%s AND id=ANY(%s) AND status = ANY(%s) AND kind = ANY(%s)",
                        (
                            self._agent_id,
                            list(settlement.inbound_ids),
                            list(_SETTLE_STATUSES),
                            [
                                InboundKind.CHAT.value,
                                InboundKind.SYSTEM_NOTE.value,
                                InboundKind.HEARTBEAT.value,
                            ],
                        ),
                    )
            for stored in positions:
                await aset_transcript_position(
                    conn, self._agent_id, self._session_id, stored, self._cli_version()
                )
        return written

    async def _append(
        self, conn: AsyncConnection, items: tuple[PlannedItem, ...]
    ) -> list[TimelineItem]:
        written: list[TimelineItem] = []
        calls: dict[str, str] = {}
        for planned in items:
            item = planned.item
            if planned.parent_tool_use_id is not None:
                parent = calls.get(planned.parent_tool_use_id)
                if parent is None:
                    parent = await aget_harness_call_item_id(
                        conn, self._agent_id, planned.parent_tool_use_id
                    )
                item = item.model_copy(update={"parent_item_id": parent})
            stored = await aappend_keyed_timeline_items(
                conn,
                self._agent_id,
                self._harness,
                [(planned.key, item)],
                projection_version=PROJECTION_VERSION,
            )
            for out in stored:
                written.append(out)
                if out.kind == "agent_code" and out.tool_use_id:
                    calls[out.tool_use_id] = out.item_id
        return written
