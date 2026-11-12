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
    aappend_keyed_timeline_items,
    aget_harness_call_item_id,
    aload_transcript_positions,
    aset_transcript_position,
)
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import InboundKind
from base.clock import Clock
from base.db.transaction import async_write_transaction
from base.lm.harness.session.transcript.drafts import Projection, SentInput
from base.lm.harness.session.transcript.entries import AnyEntry
from base.lm.harness.session.transcript.ports import RecordReader
from base.lm.harness.session.transcript.projection import Projector
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation, StaleIncarnationError

# meiva:note
# The session-scoped writer of an external agent's timeline. It lives as long as the session, not as long as a
# graph invocation, and it does one thing in a loop: when woken (a stream frame, a `session_store` callback, a
# poll) it reads every record after its stored position, projects what it read, and commits the result in one
# transaction: keyed timeline items appended in the order read, the inbound rows the entries prove delivered
# set `done`, and the new positions. A commit that fails is retried with the same plan and nothing is read in
# between, so entries are never lost to the projector's memory of having seen them; a stale incarnation ends
# the writer. A restart replays each record silently into a fresh projector (its pairing and delivery state)
# and projects only what lies past the stored position. What it commits is handed to `after_commit` once, for
# the caller to publish, capture for delivery, price and wake the graph with.
# meiva:link agent/harness_timeline/README.md

POLL_SECONDS = 3.0
RETRY_SECONDS = 2.0
FAULT_AFTER_SECONDS = 60.0
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
        self._positions: dict[str, str] = {}
        self._pending: tuple[WritePlan, dict[str, str]] | None = None
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
        projection, positions = self._project(batches)
        plan = plan_write(
            projection,
            sent_items=self._sent.items,
            inbound_ids=self._sent.inbound_ids,
            non_human_turns=frozenset(self._non_human),
            policy=self._policy,
            render_tool=self._render_tool,
        )
        self._note_turns(plan)
        self._pending = (plan, positions)
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
        for record in await self._reader.records():
            await self._replay(record)
        self._started = True

    async def _replay(self, record: str) -> None:
        everything = await self._reader.read_after(record, None)
        if everything is None:
            return
        stored = self._positions.get(record)
        tail = await self._reader.read_after(record, stored) if stored is not None else everything
        tail_keys: set[str] = {entry.key for entry in tail.entries} if tail else set()
        _ = self._projector.feed(
            entry for entry in everything.entries if entry.key not in tail_keys
        )

    async def _read_all(self) -> list[tuple[str, tuple[AnyEntry, ...], str]]:
        batches: list[tuple[str, tuple[AnyEntry, ...], str]] = []
        for record in await self._reader.records():
            batch = await self._reader.read_after(record, self._positions.get(record))
            if batch is not None:
                batches.append((record, batch.entries, batch.position))
        return batches

    def _project(
        self, batches: list[tuple[str, tuple[AnyEntry, ...], str]]
    ) -> tuple[Projection, dict[str, str]]:
        entries = [entry for _, batch, _ in batches for entry in batch]
        return self._projector.feed(entries), {record: position for record, _, position in batches}

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
        plan, positions = pending
        written = await self._commit(plan, positions)
        self._positions.update(positions)
        self._pending = None
        self._closed_turns += len(plan.closed)
        await self._after_commit(Committed(plan, tuple(written)))
        return True

    async def _commit(self, plan: WritePlan, positions: Mapping[str, str]) -> list[TimelineItem]:
        async with async_write_transaction(self._pool) as conn:
            await lock_inbound_owner(conn, self._agent_id, incarnation=self._incarnation)
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
            for record, position in positions.items():
                await aset_transcript_position(
                    conn,
                    self._agent_id,
                    self._session_id,
                    record,
                    position,
                    self._cli_version(),
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
            )
            for out in stored:
                written.append(out)
                if out.kind == "agent_code" and out.tool_use_id:
                    calls[out.tool_use_id] = out.item_id
        return written
