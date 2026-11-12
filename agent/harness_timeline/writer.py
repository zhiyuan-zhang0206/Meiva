from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.holds import (
    ABANDONED_WINDOW_ROWS,
    RELEASE_ROUNDS,
    OrphanClocks,
    restart_point,
)
from agent.harness_timeline.plan import ReplyPolicy, WritePlan, plan_write
from agent.harness_timeline.rebuild import REBUILD_MAX_ROWS, rebuild_record
from agent.harness_timeline.store import (
    PlannedTimelineItem,
    StoredPosition,
    aappend_planned_timeline_items,
    aappend_transcript_rows,
    aload_recent_agent_replies,
    aload_transcript_positions,
    aset_transcript_position,
    aset_transcript_positions,
)
from agent.harness_timeline.turns import TurnLedger
from agent.ownership.inbound import RuntimeOwnershipLostError, lock_inbound_owner
from base.agents.context.harness_timeline import TurnEndWait
from base.agents.history.harness_messages import aappend_keyed_timeline_items, ahas_harness_timeline
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import InboundKind
from base.clock import Clock
from base.db.transaction import async_write_transaction
from base.host.boundary.recent import RecentMap, RecentSet
from base.lm.harness import Usage
from base.lm.harness.session.transcript.drafts import InboundPart, Projection, SentInput
from base.lm.harness.session.transcript.entries import MAIN_RECORD
from base.lm.harness.session.transcript.ports import (
    EntryBatch,
    PositionLostError,
    RecordReader,
)
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
RETRY_MAX_SECONDS = 30.0
STACK_EVERY_FAILURES = 10
FAULT_AFTER_SECONDS = 60.0
MAX_TRANSACTION_BYTES = 8 * 1024 * 1024
MAX_TRANSACTION_ENTRIES = 5_000
CALLS_KEPT = 4_096
PROJECTION_VERSION = 1
RETIRE_WAIT_SECONDS = 10.0
NON_HUMAN_LIMIT = 1_024
_SETTLE_STATUSES = ("claimed", "pending")


@dataclass(frozen=True, slots=True)
class Committed:
    plan: WritePlan
    written: tuple[TimelineItem, ...]


@dataclass(slots=True)
class _Sent:
    items: dict[str, TimelineItem] = field(default_factory=dict[str, TimelineItem])
    refs: dict[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    inbound_ids: dict[str, tuple[int, ...]] = field(default_factory=dict[str, tuple[int, ...]])
    wire_ids: dict[str, str] = field(default_factory=dict[str, str])


@dataclass(frozen=True, slots=True)
class _Pending:
    plan: WritePlan
    batches: list[EntryBatch]
    release: bool = False


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
        max_transaction_bytes: int = MAX_TRANSACTION_BYTES,
        max_transaction_entries: int = MAX_TRANSACTION_ENTRIES,
        calls_kept: int = CALLS_KEPT,
        retire_wait_seconds: float = RETIRE_WAIT_SECONDS,
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
        self._max_bytes = max_transaction_bytes
        self._max_entries = max_transaction_entries
        self._calls_kept = calls_kept
        self._retire_wait = retire_wait_seconds
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
        self._non_human: RecentSet[str] = RecentSet(NON_HUMAN_LIMIT)
        self._history_records: frozenset[str] = frozenset()
        self._seeded: set[str] = set()
        self._held = True
        self._orphans = OrphanClocks()
        self._turns = TurnLedger()
        self._wake = asyncio.Event()
        self._stop_asked = asyncio.Event()
        self._io = asyncio.Lock()
        self._closed_turns = 0
        self._items_written = 0
        self._invalid_reply = False
        self._calls: RecentMap[str, str | None] = RecentMap(self._calls_kept)
        self._resume_at: str | None = None
        self._stopping = False
        self._retired = False
        self._release_requested = False
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

    # meiva:note
    # The spend the record tallied for the record turns the inputs `keys` were delivered in (summed when they were in
    # several), or None when none of them has closed or none has a tally. Looked up by identity, never by order: a turn
    # whose closing the record shows late, or a turn the CLI ran on its own, cannot shift which turn's spend a result
    # is compared with. The latest `CLOSED_KEPT` closed turns are kept.
    def closed_usage(self, keys: Sequence[str]) -> Usage | None:
        return self._turns.closed_usage(keys)

    # meiva:note
    # The size of everything the writer remembers, by name, and of everything the projector remembers
    # (`projector_sizes`): the bounds that keep the memory of a session of any length fixed. The writer's are: the
    # inputs registered and not yet settled (`sent_*`, dropped when the commit that settles them lands or the delivery
    # side forgets them), `non_human` (the latest `NON_HUMAN_LIMIT` turns), `turn_usage` (`TURN_USAGE_LIMIT`), the closed
    # turns' spend (`CLOSED_KEPT`), the turn each delivered input was in (`DELIVERED_KEPT`), the known calls (`CALLS_KEPT`) and one position per record
    # of the session. The projector's are stated in `projection.py`.
    def memory_sizes(self) -> dict[str, int]:
        return {
            "sent_items": len(self._sent.items),
            "sent_refs": len(self._sent.refs),
            "sent_inbound": len(self._sent.inbound_ids),
            "sent_wire": len(self._sent.wire_ids),
            "non_human": len(self._non_human),
            **self._turns.sizes(),
            "calls": len(self._calls),
            "positions": len(self._positions),
            "last_ordinal": len(self._last_ordinal),
        }

    def projector_sizes(self) -> dict[str, int]:
        return self._projector.memory_sizes()

    # meiva:note
    # The `agent_chat` items among the agent's latest `rows` timeline rows, for the effects to offer to the (idempotent)
    # capture of replies for delivery again when the writer starts.
    async def recent_replies(self, rows: int) -> list[TimelineItem]:
        async with self._pool.connection() as conn:
            return await aload_recent_agent_replies(conn, self._agent_id, rows=rows)

    def clear_fault(self) -> None:
        self._fault = None

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
        self._sent.refs[sent.key] = tuple(items)
        self._sent.inbound_ids[sent.key] = inbound_ids
        self._sent.wire_ids[sent.key] = sent.wire_id

    def forget_sent(self, key: str) -> None:
        self._projector.unregister(key)
        self._drop_sent(key)

    def _drop_sent(self, key: str) -> None:
        for ref in self._sent.refs.pop(key, ()):
            _ = self._sent.items.pop(ref, None)
        _ = self._sent.inbound_ids.pop(key, None)
        _ = self._sent.wire_ids.pop(key, None)

    def undelivered(self) -> tuple[SentInput, ...]:
        return self._projector.undelivered()

    def take_invalid_reply(self) -> bool:
        invalid, self._invalid_reply = self._invalid_reply, False
        return invalid

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stopping = True
        self._stop_asked.set()
        self._wake.set()

    # meiva:note
    # The end of a writer that did not stop in time: from this call on it starts no read and commits no plan, whatever
    # is bound to it later, and it waits up to `RETIRE_WAIT_SECONDS` for the read in flight (if any) to let go. A read
    # that is stuck past that is left to end on its own: it finds the writer retired before it commits and writes
    # nothing. A runtime that gave up waiting for its writer retires it, so that a writer of the same agent in a new
    # runtime is never one of two, and a close never waits on a read that does not come back.
    async def retire(self) -> None:
        self.stop()
        self._retired = True
        try:
            async with asyncio.timeout(self._retire_wait):
                async with self._io:
                    pass
        except TimeoutError:
            logger.warning(
                "a read of the retired transcript timeline writer is still in flight after {seconds} seconds; "
                "it commits nothing when it returns",
                event="harness_timeline_writer_retired",
                agent_id=self._agent_id,
                seconds=self._retire_wait,
            )

    # meiva:note
    # One read: a plan left uncommitted is committed first, otherwise every record is read after its position,
    # projected, planned and committed. Reads are mutually exclusive (the loop, the invocation's waits, the final
    # read and the checks the delivery side makes all come through here), so a plan is committed once, its
    # accounting runs once and two callers can never read the same bytes and overwrite each other's pending plan.
    # The lock is held for the whole read, including the effects that follow its commit.
    async def read_now(self, *, exhaustive: bool = False) -> bool:
        async with self._io:
            return await self._read_locked(exhaustive=exhaustive)

    async def _read_locked(self, *, exhaustive: bool) -> bool:
        sid = self._session_id()
        if sid is None or self._held or self._retired:
            return False
        if sid != self._sid:
            if self._releasing_old_session():
                return await self._commit_pending()
            await self._start_up(sid)
        if self._fenced:
            return False
        if self._pending is not None:
            return await self._commit_pending()
        batches = await self._read_all(sid, exhaustive=exhaustive)
        expired = self._orphans.expired(self._clock.monotonic())
        release_all, self._release_requested = self._release_requested, False
        if not batches and not expired and not release_all:
            return False
        projection = self._project(batches, release_orphans=expired, release_all=release_all)
        self._orphans.track(self._projector.unresolved_results(), self._clock.monotonic())
        plan = self._plan(projection)
        self._note_turns(plan)
        self._pending = _Pending(plan, batches)
        return await self._commit_pending()

    # meiva:note
    # Everything the writer holds back is shown now, top-level: the final act of a writer that is closing for good (its runtime
    # closes: the CLI's process goes away, the session is recycled, the agent is dropped, the host shuts down), asked once
    # after the last read. A writer that only faulted and will be started again by the next hosted turn is not closing and is
    # not asked. What was held is not lost to a stop in any case (it is re-read from the stored rows and held again); this
    # is what makes it appear when no turn will ever come.
    async def release_held(self) -> None:
        async with self._io:
            self._release_requested = True
            for _ in range(RELEASE_ROUNDS):
                _ = await self._read_locked(exhaustive=True)
                if (
                    self._pending is None
                    and not self._release_requested
                    and not self._holds_anything()
                ):
                    return

    def _holds_anything(self) -> bool:
        return bool(self._projector.unresolved_results() or self._projector.unlinked_records())

    # meiva:note
    # The session was replaced by one with another id: what the old session still holds can never be linked, so it is shown now
    # (committed under the old session's id, with no positions to move) before the writer starts over for the new one.
    def _plan(self, projection: Projection) -> WritePlan:
        return plan_write(
            projection,
            sent_items=self._sent.items,
            inbound_ids=self._sent.inbound_ids,
            non_human_turns=frozenset(self._non_human),
            policy=self._policy,
            render_tool=self._render_tool,
        )

    def _releasing_old_session(self) -> bool:
        if self._sid is None:
            return False
        if self._pending is None and self._holds_anything():
            self._pending = self._release_plan()
        return self._pending is not None and self._pending.release

    def _release_plan(self) -> _Pending:
        projection = self._projector.feed([], release_held=True)
        plan = self._plan(projection)
        return _Pending(plan, [], release=True)

    # meiva:note
    # The turn the stream reports ended is waited for here by identity: the record turns the inputs `keys` (the ones
    # the stream says were in that turn) were delivered in, and every one of them closed. The writer knows each
    # delivered input's turn from the record row that settled it, and each closing by the turn it names, so a closing
    # that shows up late is the closing of its own turn and no other turn's wait ends early, however many turns the
    # CLI ran on its own in between (they hold none of our inputs and are not waited for: with no `keys` the wait
    # is over at once). A turn whose closing the record never shows ends the wait quietly (event
    # `harness_timeline_turn_end_unseen`, naming the keys and turns) and the turn is simply not closed yet when the
    # record shows it.
    async def wait_for_turn_end(
        self, keys: Sequence[str], *, quiet_seconds: float, ceiling_seconds: float
    ) -> TurnEndWait:
        if not keys:
            return TurnEndWait.CLOSED
        deadline = self._clock.monotonic() + ceiling_seconds
        last_activity = self._clock.monotonic()
        while True:
            while await self.read_now():
                last_activity = self._clock.monotonic()
            if self._turns.turns_closed(keys):
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
                    keys=list(keys),
                    turns=[self._turns.turn_of(key) for key in keys],
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
        async with self._io:
            if self._retired:
                return
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

    # meiva:note
    # The loop. A failed read or commit is tried again after a wait that doubles from `RETRY_SECONDS` to
    # `RETRY_MAX_SECONDS`; the first failure and every `STACK_EVERY_FAILURES`-th after it are logged with their stack,
    # the others in one line. Failing for `FAULT_AFTER_SECONDS` without a success stops the loop with a fault. A
    # `stop` ends the loop after one last read, wakes a wait for the next retry and does not wait for it, and a stop
    # asked for before the loop ran is not forgotten.
    async def run(self) -> None:
        failing_since: float | None = None
        failures = 0
        while not self._stopping:
            try:
                if await self.read_now():
                    failing_since, failures = None, 0
                    await asyncio.sleep(0)
                    continue
                failing_since, failures = None, 0
            except Exception as error:
                failures += 1
                failing_since = (
                    failing_since if failing_since is not None else self._clock.monotonic()
                )
                self._log_failure(error, failures)
                if self._clock.monotonic() - failing_since >= FAULT_AFTER_SECONDS:
                    self._fault = error
                    logger.error(
                        "the harness timeline writer gave up after repeated failures",
                        event="harness_timeline_writer_faulted",
                        agent_id=self._agent_id,
                    )
                    return
                with suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_asked.wait(),
                        min(RETRY_SECONDS * 2 ** (failures - 1), RETRY_MAX_SECONDS),
                    )
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), POLL_SECONDS)
            self._wake.clear()
        if self._sid is not None and self._fault is None and not self._fenced:
            _ = await self.read_now(exhaustive=True)

    def _log_failure(self, error: Exception, failures: int) -> None:
        message = "the harness timeline writer could not read or commit"
        if failures % STACK_EVERY_FAILURES == 1:
            logger.opt(exception=True).warning(
                message,
                event="harness_timeline_writer_retry",
                agent_id=self._agent_id,
                failures=failures,
            )
        else:
            logger.warning(
                message,
                event="harness_timeline_writer_retry",
                agent_id=self._agent_id,
                failures=failures,
                error=repr(error),
            )

    # meiva:note
    # Start over for a session: its stored positions, whether it starts with history (a session with no stored
    # position whose agent already has a timeline) and, for such a start, which records were already on disk, then the
    # projector's state rebuilt from the stored rows into a projector of its own. Nothing the writer holds changes
    # until the rebuild has finished, so a rebuild that fails leaves the writer as it was and is made again at the next
    # read; a new session id drops the plan left uncommitted for the old one and everything else the old session
    # established, keeping only the inputs registered and not yet shown.
    async def _start_up(self, sid: str) -> None:
        async with self._pool.connection() as conn:
            positions = await aload_transcript_positions(conn, self._agent_id, sid)
            skip_history = not positions and await ahas_harness_timeline(conn, self._agent_id)
        history: frozenset[str] = (
            frozenset(await self._reader.records()) if skip_history else frozenset()
        )
        projector = Projector()
        for sent in self._projector.undelivered():
            projector.register(sent)
        last_ordinal: dict[str, int] = {}
        for record, stored in positions.items():
            last = await rebuild_record(
                self._pool,
                self._reader,
                projector,
                agent_id=self._agent_id,
                sid=sid,
                record=record,
                after_ordinal=stored.rebuild_ordinal,
                max_rows=self._rebuild_max,
            )
            if last is not None:
                last_ordinal[record] = last
        self._projector = projector
        self._positions = positions
        self._last_ordinal = last_ordinal
        self._history_records = history
        self._seeded = set()
        self._pending = None
        self._non_human = RecentSet(NON_HUMAN_LIMIT)
        self._turns = TurnLedger()
        self._calls = RecentMap(self._calls_kept)
        self._resume_at = None
        self._orphans = OrphanClocks()
        self._orphans.track(projector.unresolved_results(), self._clock.monotonic())
        self._sid = sid

    # meiva:note
    # What one transaction may hold: records are read in turn, each at most one reader batch, until a read has brought
    # `MAX_TRANSACTION_BYTES` of the CLI's bytes or `MAX_TRANSACTION_ENTRIES` entries; the records not yet read wait for
    # the next transaction, which starts with them. The main record is always read first and never waits: it alone holds
    # the links that tell a sub-agent's output which call it nests under, so catching up never reads a sub-agent's
    # output ahead of the main record's own progress (the sub-agent's output is held for its link only for a bounded
    # time). Only the sub-agents' records rotate, and every transaction reads at least one of them, so a busy main
    # record cannot starve them. However many sub-agents have records, a transaction (and the lock it holds on the
    # agent's row) is bounded.
    async def _read_all(self, sid: str, *, exhaustive: bool) -> list[EntryBatch]:
        batches: list[EntryBatch] = []
        size = entries = 0
        records = list(await self._reader.records(exhaustive=exhaustive))
        others = [record for record in records if record != MAIN_RECORD]
        if self._resume_at in others:
            at = others.index(self._resume_at)
            others = others[at:] + others[:at]
        self._resume_at = None
        ordered = [record for record in records if record == MAIN_RECORD] + others
        took_other = False
        for record in ordered:
            if record != MAIN_RECORD:
                if took_other and (size >= self._max_bytes or entries >= self._max_entries):
                    self._resume_at = record
                    break
                took_other = True
            batch = await self._read_record(sid, record)
            if batch is not None:
                batches.append(batch)
                size += batch.size
                entries += len(batch.entries)
        return batches

    async def _read_record(self, sid: str, record: str) -> EntryBatch | None:
        stored = self._positions.get(record)
        if stored is None and record in self._history_records and record not in self._seeded:
            stored = await self._seed_past_history(sid, record)
            self._seeded.add(record)
        try:
            return await self._reader.read_after(record, stored.position if stored else None)
        except PositionLostError as lost:
            await self._reseat(sid, record, stored, lost)
            return None

    # meiva:note
    # A position the record no longer holds (the file was replaced, truncated or rewritten under it) is not a failure to
    # retry: the same read would fail the same way for ever and the inputs have long been handed to the CLI. The
    # record is put at its current end, as a switch to a session with history does, and reading goes on from there; the
    # event `harness_timeline_position_lost` names the agent, session, record and the position that was lost. What the CLI
    # wrote between the lost position and the new one is not on the timeline; it can be projected afterwards from the file
    # that is still on disk (the README says how). The new position counts its rows from above the highest ordinal
    # stored for the record, so the rows of the rewritten file never take the ordinals of rows already stored (a row
    # whose ordinal is stored is not written again). A record whose file is gone altogether has no end to go to: it is
    # no longer read.
    async def _reseat(
        self, sid: str, record: str, lost_at: StoredPosition | None, error: PositionLostError
    ) -> None:
        floor = max(self._last_ordinal.get(record, -1), lost_at.rebuild_ordinal if lost_at else -1)
        end = await self._reader.position_before(record, (), after_ordinal=floor)
        logger.warning(
            "the transcript no longer holds the position stored for a record; reading goes on from the "
            "record's end",
            event="harness_timeline_position_lost",
            agent_id=self._agent_id,
            session_id=sid,
            record=record,
            position=None if lost_at is None else lost_at.position,
            reseated_at=end,
            reason=str(error),
        )
        if end is None:
            _ = self._positions.pop(record, None)
            return
        stored = StoredPosition(record, end, lost_at.rebuild_ordinal if lost_at else -1)
        async with async_write_transaction(self._pool) as conn:
            await lock_inbound_owner(conn, self._agent_id, incarnation=self._incarnation)
            await aset_transcript_position(conn, self._agent_id, sid, stored, self._cli_version())
        self._positions[record] = stored

    # meiva:note
    # Only a record that was on disk when the writer started is history to skip, and it is decided once for the
    # record: a sub-agent that appears after the start is read from its first row, however quickly it ran. The main
    # record is read from the first input this process sent, found by an id only that input has: an input with an
    # inbox row (its key names the row, unique to the agent), never a synthetic one whose key repeats from session to
    # session and may well be in the history.
    async def _seed_past_history(self, sid: str, record: str) -> StoredPosition | None:
        wire_ids = (
            tuple(
                wire for key, wire in self._sent.wire_ids.items() if self._sent.inbound_ids.get(key)
            )
            if record == MAIN_RECORD
            else ()
        )
        position = await self._reader.position_before(record, wire_ids)
        if position is None:
            return None
        stored = StoredPosition(record, position, -1)
        async with async_write_transaction(self._pool) as conn:
            await lock_inbound_owner(conn, self._agent_id, incarnation=self._incarnation)
            await aset_transcript_position(conn, self._agent_id, sid, stored, self._cli_version())
        self._positions[record] = stored
        return stored

    def _project(
        self,
        batches: list[EntryBatch],
        *,
        release_orphans: frozenset[str],
        release_all: bool = False,
    ) -> Projection:
        return self._projector.feed(
            [entry for batch in batches for entry in batch.entries],
            release_held=release_all,
            release_orphans=release_orphans,
        )

    def _note_turns(self, plan: WritePlan) -> None:
        for turn in plan.observed:
            if turn.origin.value != "input":
                self._non_human.add(turn.turn)
            else:
                self._non_human.discard(turn.turn)

    async def _commit_pending(self) -> bool:
        pending = self._pending
        sid = self._sid
        if pending is None or sid is None or self._retired:
            return False
        plan = pending.plan
        positions = self._next_positions(pending)
        try:
            written, learnt = await self._commit(sid, plan, pending.batches, positions)
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
        for call, item_id in learnt.items():
            self._calls[call] = item_id
        for settlement in plan.settlements:
            self._drop_sent(settlement.input_key)
        self._account(plan, len(written))
        await self._after_commit(Committed(plan, tuple(written)))
        return True

    def _account(self, plan: WritePlan, written: int) -> None:
        self._items_written += written
        self._closed_turns += len(plan.closed)
        self._invalid_reply = self._invalid_reply or bool(plan.invalid_reply_turns)
        self._turns.record(plan)

    def _next_positions(self, pending: _Pending) -> list[StoredPosition]:
        quiet = bool(pending.plan.closed) and self._quiet()
        result: list[StoredPosition] = []
        for batch in pending.batches:
            previous = self._positions.get(batch.record)
            last = (
                batch.rows[-1].ordinal if batch.rows else self._last_ordinal.get(batch.record, -1)
            )
            before = previous.rebuild_ordinal if previous else -1
            rebuild = self._restart(batch.record, last, before) if quiet else before
            result.append(StoredPosition(batch.record, batch.position, rebuild))
        return result

    def _restart(self, record: str, last: int, before: int) -> int:
        oldest = self._projector.oldest_abandoned(record, since=last - ABANDONED_WINDOW_ROWS + 1)
        return restart_point(last, before, oldest)

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
    ) -> tuple[list[TimelineItem], dict[str, str | None]]:
        async with async_write_transaction(self._pool) as conn:
            await lock_inbound_owner(conn, self._agent_id, incarnation=self._incarnation)
            _ = await aappend_transcript_rows(
                conn,
                self._agent_id,
                sid,
                [(row.record, row.ordinal, row.row) for batch in batches for row in batch.rows],
            )
            written, learnt = await aappend_planned_timeline_items(
                conn,
                self._agent_id,
                self._harness,
                [PlannedTimelineItem(i.key, i.item, i.parent_tool_use_id) for i in plan.items],
                calls=self._known_calls(plan),
                projection_version=PROJECTION_VERSION,
            )
            settled = sorted({i for s in plan.settlements for i in s.inbound_ids})
            if settled:
                _ = await conn.execute(
                    "UPDATE inbound_messages SET status='done' "
                    "WHERE agent_id=%s AND id=ANY(%s) AND status = ANY(%s) AND kind = ANY(%s)",
                    (
                        self._agent_id,
                        settled,
                        list(_SETTLE_STATUSES),
                        [
                            InboundKind.CHAT.value,
                            InboundKind.SYSTEM_NOTE.value,
                            InboundKind.HEARTBEAT.value,
                        ],
                    ),
                )
            await aset_transcript_positions(
                conn, self._agent_id, sid, positions, self._cli_version()
            )
        return written, learnt

    def _known_calls(self, plan: WritePlan) -> dict[str, str | None]:
        wanted = {i.parent_tool_use_id for i in plan.items if i.parent_tool_use_id is not None}
        return {call: self._calls.get(call) for call in wanted if call in self._calls}
