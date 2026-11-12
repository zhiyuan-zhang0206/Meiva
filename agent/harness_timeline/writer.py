from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import PlannedItem, ReplyPolicy, WritePlan, plan_write
from agent.ownership.inbound import RuntimeOwnershipLostError, lock_inbound_owner
from base.agents.context.harness_timeline import TurnEndWait
from base.agents.history.harness_messages import (
    StoredPosition,
    aappend_keyed_timeline_items,
    aappend_transcript_rows,
    aget_harness_call_item_id,
    ahas_harness_timeline,
    aload_transcript_positions,
    aload_transcript_row_floor,
    aload_transcript_rows,
    aset_transcript_position,
)
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import InboundKind
from base.clock import Clock
from base.db.transaction import async_write_transaction
from base.host.boundary.json_values import is_object_dict
from base.lm.harness import Usage
from base.lm.harness.session.transcript.drafts import InboundPart, Projection, SentInput
from base.lm.harness.session.transcript.entries import MAIN_RECORD
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
# memory of having seen them. A restart rebuilds the projector's state (pairing, delivery) from the stored rows
# after each record's rebuild ordinal, in bounded batches and without touching the CLI's file, and projects
# only what lies past the stored position. What it commits is handed to `after_commit` once, for the caller to
# publish, capture for delivery, price and wake the graph with.
#
# The session id is asked for at every read (it is known only once the CLI has started the session) and a new
# one starts the writer over for that session. The runtime incarnation is whatever `bind_incarnation` last gave
# it: the host binds the admitted one at every hosted turn, and the row's generation survives from one turn to
# the next while the same host owns it, so a writer that is idle between turns still holds the one the row
# carries. A commit refused by the owner lock fences the writer: it writes nothing and keeps its plan until a
# different incarnation is bound, so a late rebinding is never a lost write and a runtime another host took
# over never writes. Reads are repeated while they bring something, so catching up on a long record is a
# series of bounded transactions.
#
# The first start of a session that has no stored position, for an agent whose timeline already has items (an
# agent that was on the event path, a fork whose record starts with the parent's history), does not project the
# history that was there: the main record is read from the row of the first input this process sent (found by
# its wire id) and every sub-agent record already on disk is read from its end; with no input registered yet
# the position is the end. An agent with no timeline yet reads everything.
# meiva:link agent/harness_timeline/README.md

POLL_SECONDS = 3.0
RETRY_SECONDS = 2.0
FAULT_AFTER_SECONDS = 60.0
PROJECTION_VERSION = 1
REBUILD_BATCH_ROWS = 500
REBUILD_MAX_ROWS = 20_000
_SETTLE_STATUSES = ("claimed", "pending")


@dataclass(frozen=True, slots=True)
class Committed:
    plan: WritePlan
    written: tuple[TimelineItem, ...]


@dataclass(slots=True)
class _Sent:
    items: dict[str, TimelineItem] = field(default_factory=dict[str, TimelineItem])
    inbound_ids: dict[str, tuple[int, ...]] = field(default_factory=dict[str, tuple[int, ...]])
    wire_ids: dict[str, str] = field(default_factory=dict[str, str])


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
        session_id: str | Callable[[], str | None],
        wire_id: Callable[[str], str] = lambda key: key,
        marker_id: Callable[[], str],
        rebuild_max_rows: int = REBUILD_MAX_ROWS,
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
        self._session_id = (lambda: session_id) if isinstance(session_id, str) else session_id
        self._sid: str | None = None
        self._wire_id = wire_id
        self._marker_id = marker_id
        self._rebuild_max = rebuild_max_rows
        self._incarnation = incarnation
        self._fenced = False
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
        self._skip_history = False
        self._held = True
        self._closed_usage: Usage | None = None
        self._wake = asyncio.Event()
        self._closed_turns = 0
        self._items_written = 0
        self._invalid_reply = False
        self._turn_usage: dict[str, Usage] = {}
        self._stopping = False
        self._fault: Exception | None = None

    @property
    def closed_turns(self) -> int:
        return self._closed_turns

    @property
    def items_written(self) -> int:
        return self._items_written

    @property
    def fault(self) -> Exception | None:
        return self._fault

    @property
    def fenced(self) -> bool:
        return self._fenced

    @property
    def closed_usage(self) -> Usage | None:
        return self._closed_usage

    def clear_fault(self) -> None:
        self._fault = None
        self._stopping = False

    def start_reading(self) -> None:
        self._held = False
        self._wake.set()

    def bind_incarnation(self, incarnation: RuntimeIncarnation | None) -> None:
        if incarnation != self._incarnation:
            self._incarnation = incarnation
            self._fenced = False
        self._wake.set()

    def register_input(
        self,
        key: str,
        text: str,
        parts: Sequence[tuple[str, TimelineItem]],
        inbound_ids: tuple[int, ...],
    ) -> None:
        self.forget_sent(key)
        sent = SentInput(
            key=key,
            wire_id=self._wire_id(key),
            text=text,
            parts=tuple(InboundPart(ref=ref, text=item.payload) for ref, item in parts),
        )
        self.register_sent(sent, items=dict(parts), inbound_ids=inbound_ids)

    def register_sent(
        self, sent: SentInput, *, items: Mapping[str, TimelineItem], inbound_ids: tuple[int, ...]
    ) -> None:
        self._projector.register(sent)
        self._sent.items.update(items)
        self._sent.inbound_ids[sent.key] = inbound_ids
        self._sent.wire_ids[sent.key] = sent.wire_id

    def forget_sent(self, key: str) -> None:
        self._projector.unregister(key)
        self._sent.inbound_ids.pop(key, None)
        self._sent.wire_ids.pop(key, None)

    def undelivered(self) -> tuple[SentInput, ...]:
        return self._projector.undelivered()

    def take_invalid_reply(self) -> bool:
        invalid, self._invalid_reply = self._invalid_reply, False
        return invalid

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._wake.set()

    async def read_now(self) -> bool:
        sid = self._session_id()
        if sid is None or self._held:
            return False
        if sid != self._sid:
            await self._start_up(sid)
        if self._fenced:
            return False
        if self._pending is not None:
            return await self._commit_pending()
        batches = await self._read_all(sid)
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
            while await self.read_now():
                last_activity = self._clock.monotonic()
            if self._closed_turns > closed_before:
                return TurnEndWait.CLOSED
            now = self._clock.monotonic()
            outcome = (
                TurnEndWait.CEILING
                if now >= deadline
                else TurnEndWait.QUIET
                if now - last_activity >= quiet_seconds
                else None
            )
            if outcome is not None:
                logger.warning(
                    "the transcript showed no closing entry for the turn that ended",
                    event="harness_timeline_turn_end_unseen",
                    agent_id=self._agent_id,
                    outcome=outcome.value,
                    waited_seconds=round(ceiling_seconds - (deadline - now), 3),
                )
                return outcome
            await asyncio.sleep(min(0.1, quiet_seconds))

    async def write_marker(self, text: str, *, error: bool) -> None:
        marker = TimelineItem(
            item_id="pending",
            kind="system_marker",
            payload=f"{'error' if error else 'status'}:{text}",
            created_at=self._clock.now().isoformat(),
        )
        async with async_write_transaction(self._pool) as conn:
            await lock_inbound_owner(conn, self._agent_id, incarnation=self._incarnation)
            written = await aappend_keyed_timeline_items(
                conn,
                self._agent_id,
                self._harness,
                [(f"marker:{self._marker_id()}", marker)],
                projection_version=PROJECTION_VERSION,
            )
        self._items_written += len(written)
        await self._after_commit(Committed(WritePlan(), tuple(written)))

    async def run(self) -> None:
        failing_since: float | None = None
        while not self._stopping:
            try:
                if await self.read_now():
                    failing_since = None
                    await asyncio.sleep(0)
                    continue
                failing_since = None
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
                    logger.error(
                        "the harness timeline writer gave up after repeated failures",
                        event="harness_timeline_writer_faulted",
                        agent_id=self._agent_id,
                    )
                    return
                await asyncio.sleep(RETRY_SECONDS)
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
            self._wake.clear()
        if self._sid is not None and self._fault is None and not self._fenced:
            _ = await self.read_now()

    async def _start_up(self, sid: str) -> None:
        async with self._pool.connection() as conn:
            self._positions = await aload_transcript_positions(conn, self._agent_id, sid)
            self._skip_history = not self._positions and await ahas_harness_timeline(
                conn, self._agent_id
            )
        self._last_ordinal = {}
        self._sid = sid
        for record, stored in self._positions.items():
            await self._rebuild(sid, record, stored.rebuild_ordinal)

    async def _rebuild(self, sid: str, record: str, after_ordinal: int) -> None:
        cursor = await self._rebuild_floor(sid, record, after_ordinal)
        while True:
            async with self._pool.connection() as conn:
                loaded = await aload_transcript_rows(
                    conn,
                    self._agent_id,
                    sid,
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

    async def _rebuild_floor(self, sid: str, record: str, after_ordinal: int) -> int:
        async with self._pool.connection() as conn:
            floor = await aload_transcript_row_floor(
                conn,
                self._agent_id,
                sid,
                record,
                after_ordinal=after_ordinal,
                keep=self._rebuild_max,
            )
        if floor is None:
            return after_ordinal
        logger.warning(
            "a turn has gone more than {limit} stored rows without a quiet point; the projector "
            "is rebuilt from the latest {limit} only, so the output of a call that began earlier "
            "is held and not shown",
            event="harness_timeline_rebuild_bounded",
            agent_id=self._agent_id,
            record=record,
            limit=self._rebuild_max,
            rebuild_ordinal=after_ordinal,
            resumed_after=floor,
        )
        return floor

    async def _read_all(self, sid: str) -> list[EntryBatch]:
        batches: list[EntryBatch] = []
        for record in await self._reader.records():
            stored = self._positions.get(record)
            if stored is None and self._skip_history:
                stored = await self._seed_past_history(sid, record)
            batch = await self._reader.read_after(record, stored.position if stored else None)
            if batch is not None:
                batches.append(batch)
        return batches

    async def _seed_past_history(self, sid: str, record: str) -> StoredPosition | None:
        wire_ids = tuple(self._sent.wire_ids.values()) if record == MAIN_RECORD else ()
        position = await self._reader.position_before(record, wire_ids)
        if position is None:
            return None
        stored = StoredPosition(record, position, -1)
        async with async_write_transaction(self._pool) as conn:
            await lock_inbound_owner(conn, self._agent_id, incarnation=self._incarnation)
            await aset_transcript_position(conn, self._agent_id, sid, stored, self._cli_version())
        self._positions[record] = stored
        return stored

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
        sid = self._sid
        if pending is None or sid is None:
            return False
        plan = pending.plan
        positions = self._next_positions(pending)
        try:
            written = await self._commit(sid, plan, pending.batches, positions)
        except (StaleIncarnationError, RuntimeOwnershipLostError):
            self._fenced = True
            logger.warning(
                "the harness timeline writer no longer owns the agent's runtime row; it writes "
                "nothing until a different incarnation is bound",
                event="harness_timeline_fenced",
                agent_id=self._agent_id,
            )
            return False
        self._positions.update({stored.record: stored for stored in positions})
        for batch in pending.batches:
            if batch.rows:
                self._last_ordinal[batch.record] = batch.rows[-1].ordinal
        self._pending = None
        self._account(plan, len(written))
        await self._after_commit(Committed(plan, tuple(written)))
        return True

    def _account(self, plan: WritePlan, written: int) -> None:
        self._items_written += written
        self._closed_turns += len(plan.closed)
        self._invalid_reply = self._invalid_reply or bool(plan.invalid_reply_turns)
        for spend in plan.usage:
            if spend.record == MAIN_RECORD and spend.turn is not None:
                self._turn_usage[spend.turn] = (
                    self._turn_usage.get(spend.turn, Usage()) + spend.usage
                )
        for closed in plan.closed:
            self._closed_usage = self._turn_usage.pop(closed.turn, None)

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
        self,
        sid: str,
        plan: WritePlan,
        batches: list[EntryBatch],
        positions: list[StoredPosition],
    ) -> list[TimelineItem]:
        async with async_write_transaction(self._pool) as conn:
            await lock_inbound_owner(conn, self._agent_id, incarnation=self._incarnation)
            _ = await aappend_transcript_rows(
                conn,
                self._agent_id,
                sid,
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
                    conn, self._agent_id, sid, stored, self._cli_version()
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
