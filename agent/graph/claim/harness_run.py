from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from typing import Literal

from psycopg_pool import PoolTimeout
from pydantic import TypeAdapter

from agent import state as _state
from agent.db import ClaimedInbound
from agent.graph.claim._dispatch import is_human_wake_item, prepare_steered_inbound
from agent.graph.claim.harness_activity import (
    TRANSCRIPT_END_CEILING_SECONDS,
    TurnSink,
    log_turn_usage,
)
from agent.graph.claim.harness_foreground import (
    Commit,
    Foreground,
    Handed,
    Reconciled,
    Requeue,
    Settlement,
    event_generation,
    merge_handed,
    plan_wake,
)
from agent.graph.claim.harness_inbox import (
    INPUT_KEY_FIELD,
    apply_reconciled,
    claimed_deliverable,
    commit_handed,
    defer_reconciliation,
    is_deferred,
    is_passive,
    mark_input_group,
    mark_unconfirmed,
    publish_inbound,
    reconcile_handed,
    recovery_notes_first,
    requeue_handed,
    reserve_head_batch,
    timeline_items_of,
)
from agent.graph.claim.harness_render import (
    native_images,
    render_message,
    render_messages,
    skipped_images_note,
)
from agent.graph.claim.timeline import append_turn_timeline
from agent.graph.claim.turn_stamp import persist_last_active
from agent.graph.hooks.node import HOOK_NOTES_KEY
from agent.graph.hooks.question_context import read_agent_open_questions
from agent.graph.hooks.user_delivery import delivery_end_reminder
from agent.graph.interrupt import (
    ModelInterruptedError,
)
from agent.messages.reply_visibility import reply_visibility_note
from base.agents.context import MeivaContext
from base.agents.context.harness_timeline import TimelineSession
from base.agents.history.timeline import build_timeline_items
from base.agents.history.timeline_item import TimelineItem
from base.agents.questions.render import reinjection_text
from base.events.live.projection import LLMDone
from base.events.live.publisher import AgentEventPublisher
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    EventSubscription,
    HarnessClosedError,
    HarnessInput,
    HarnessNotDeliveredError,
    HarnessSetupError,
    ImageBlock,
    ReasoningDelta,
    ReasoningText,
    SubagentText,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnCompleted,
    completed_turn,
    fault_of_event,
)
from base.lm.harness.engine import TurnEngine
from base.log import logger

HarnessGoto = Literal["claim"]
_TIMELINE_ITEMS = TypeAdapter(list[TimelineItem])

# meiva:note
# Delivery is woken by the host's inbound wake, so the queue is not polled while a turn runs. The
# fallback only bounds the cost of a lost wake. After a refusal (Antigravity refuses an input for a
# conversation that is just starting or just ending) the same input is not sent again before the
# backoff has passed: the next wake or fallback tick retries, never a loop.
# meiva:link agent/graph/docs/harness-turn.md
DELIVERY_FALLBACK_SECONDS = 15.0
REDELIVER_BACKOFF_SECONDS = 2.0
POOL_RETRY_SECONDS = 1.0


# meiva:note
# The recovery input after a crash: a statement of fact, the agent decides what to do. It is sent only when no
# other recovery input (a restart note, a context reset) is already on its way.
# meiva:link agent/graph/docs/harness-foreground.md
RESUME_AFTER_RESTART = (
    "The process running you was restarted while you were handling the messages above, so the "
    "turn you were in was interrupted. Continue from where you stopped."
)


# meiva:note
# One invocation's foreground: deliver the inbox, settle what the harness reports.
#
# Two tasks share it, both on one event loop and both yielding only at awaits: the event reader folds
# each event into the pure `Foreground` and writes its settlements, and the delivery loop hands the head
# of the inbox over when it can and decides when the invocation may end.
class ForegroundRun:
    def __init__(
        self,
        ctx: MeivaContext,
        agent_id: int,
        bridge: TurnEngine,
        projector: TurnSink,
        state: _state.AgentState,
        publisher: AgentEventPublisher,
        timeline: TimelineSession | None = None,
    ) -> None:
        self._ctx = ctx
        self._timeline = timeline
        self._sent_keys: set[str] = set()
        self._record_waited = 0.0
        self._agent_id = agent_id
        self._bridge = bridge
        self._projector = projector
        self._publisher = publisher
        self._baseline = state.user_delivery_baseline
        self.fg = Foreground()
        self._synthetic: list[tuple[Handed, str]] = []
        self._prompts: dict[str, str] = {}
        self._folded: dict[str, tuple[tuple[Handed, str], ...]] = {}
        self._images: dict[str, tuple[ImageBlock, ...]] = {}
        for raw in state.harness_inputs:
            key, prompt = str(raw["key"]), str(raw["prompt"])
            if not prompt.strip():
                raise ValueError(f"synthetic input {key!r} has no text")
            items = _TIMELINE_ITEMS.validate_python(raw.get("timeline_items", []))
            self._synthetic.append((Handed(key, None, tuple(items)), prompt))
        self.last_key = f"inbound-turn-{agent_id}"
        self.interrupted = False
        self._correcting = False
        self._turn_ended = False
        self._streamed: dict[str, list[str]] = {}
        self._changed = asyncio.Event()
        self._backoff_until = 0.0
        self._failure: Exception | None = None
        # meiva:note
        # Inputs the harness has settled whose settlement is not yet written. They are no longer in `fg`, but
        # they are still unsettled as far as the database goes: the invocation must not end (nor a cancelled
        # write be forgotten) while one is in flight.
        # meiva:link agent/graph/docs/harness-foreground.md
        self._settling: list[Handed] = []
        self._kept: set[str] = set()
        self._resumed = False
        self._subscription: EventSubscription | None = None
        self._reading = False

    def _unsettled(self) -> tuple[Handed, ...]:
        return (*self.fg.handed, *self._settling)

    # meiva:note
    # The invocation failed or was interrupted with inputs handed over and unsettled. The session is
    # retired first, so a CLI that still held an input in a queue of its own can no longer take it
    # later; then the harness's durable record decides each input: in the context (settled like an
    # acknowledgement), absent (back to the queue, delivered again in its original order), stored and
    # still to be delivered by the harness itself or beyond the record's knowledge. When the record
    # does not exist, or the session cannot be retired, nothing is guessed and the inputs are consumed
    # with the uncertainty marker. A failure to read an existing record (I/O, an interface change)
    # leaves the rows claimed: the next wake reconciles them again. Returns the inbox rows consumed
    # with the marker, the only ones a lost-turn note may name.
    # meiva:link agent/graph/docs/harness-foreground.md
    async def retire_unconfirmed(self) -> list[int]:
        unsettled = self._unsettled()
        if not unsettled:
            return []
        if self._timeline is not None:
            return await self._retire_by_record(self._timeline, unsettled)
        try:
            plan = await self._reconcile_after_failure(unsettled)
        except Exception:
            logger.opt(exception=True).warning(
                "could not read the harness's record; the inputs stay claimed to be reconciled "
                "at the next wake",
                event="harness_reconcile_failed",
                agent_id=self._agent_id,
                keys=[h.key for h in unsettled],
                where="after_failure",
            )
            return []
        if plan is None:
            return await mark_unconfirmed(self._ctx, self._agent_id, self._projector, unsettled)
        return await self._apply(plan)

    async def _reconcile_after_failure(self, unsettled: Sequence[Handed]) -> Reconciled | None:
        try:
            await self._bridge.retire(reason="inputs handed to a failed run need reconciling")
        except Exception:
            logger.opt(exception=True).warning(
                "could not retire the harness session before reconciling its inputs",
                event="harness_reconcile_failed",
                agent_id=self._agent_id,
                keys=[h.key for h in unsettled],
                where="retire",
            )
            return None
        return await reconcile_handed(
            self._bridge, self._agent_id, unsettled, where="after_failure"
        )

    async def _apply(self, plan: Reconciled) -> list[int]:
        marked = await apply_reconciled(self._ctx, self._agent_id, self._projector, plan)
        if plan.requeue:
            self._changed.set()
        return marked

    # meiva:note
    # The first thing a run does after the session is up: inputs an earlier process handed to the
    # harness and left claimed (a crash, an abort, a restart) are reconciled before anything new is
    # delivered. Absent ones return to the queue at their original position and are sent in order
    # behind the recovery input. Ones the record shows in the context are settled, and because the
    # turn they were in died with the process, a note says so (unless another recovery input is
    # already on its way): without it the agent would have a message, no reply and nothing to prompt
    # one. Ones the harness stored itself are adopted and the same note makes the CLI deliver its
    # queue; if one has still not appeared when that turn is done, it is reconciled once more and, if
    # the record still has it undelivered, left claimed and marked so it does not wake the agent again.
    # Without a record, they are consumed with the uncertainty marker. An unreadable record leaves
    # them claimed and fails the run, to be retried at the next wake.
    # meiva:link agent/graph/docs/harness-foreground.md
    async def _adopt_claimed(self) -> None:
        rows = await claimed_deliverable(self._ctx, self._agent_id, include_deferred=True)
        if self._timeline is not None:
            await self._adopt_by_record(self._timeline, rows)
            return
        if not rows:
            return
        handed = self._grouped(rows)
        plan = await reconcile_handed(self._bridge, self._agent_id, handed, where="session_start")
        if plan is None:
            _ = await mark_unconfirmed(self._ctx, self._agent_id, self._projector, handed)
            return
        deferred = {r.id for r in rows if is_deferred(r)}
        keep = tuple(h for h in plan.keep if not deferred.intersection(h.row_ids))
        _ = await self._apply(plan)
        self._queue_resume_note((*plan.commit, *keep))
        if keep:
            self.fg = self.fg.hand(keep)
            self._kept = {h.key for h in keep}
            self._bridge.adopt([h.key for h in keep])

    # meiva:note
    # Claimed rows as the inputs they were handed over as: rows marked with the same input key are
    # one input again (a batch handed over as one), every other row is its own.
    def _grouped(self, rows: Sequence[ClaimedInbound]) -> list[Handed]:
        return [handed for handed, _ in self._grouped_with_text(rows)]

    def _grouped_with_text(self, rows: Sequence[ClaimedInbound]) -> list[tuple[Handed, str]]:
        by_key: dict[str, list[ClaimedInbound]] = {}
        for row in rows:
            key = row.payload.get(INPUT_KEY_FIELD) if row.payload else None
            by_key.setdefault(key if isinstance(key, str) else f"inbound-{row.id}", []).append(row)
        grouped: list[tuple[Handed, str]] = []
        for key, group in by_key.items():
            pairs = [(self._handed_of(row), is_passive(row)) for row in group]
            merged = merge_handed([(handed, passive) for (handed, _), passive in pairs])
            grouped.append((replace(merged, key=key), "\n\n".join(text for (_, text), _ in pairs)))
        return grouped

    def _queue_resume_note(self, settled: Sequence[Handed]) -> None:
        if not settled or self._synthetic:
            return
        newest = max((i for h in settled for i in h.row_ids), default=0)
        self._synthetic.append(
            (Handed(f"reconciled-resume-{newest}", None, ()), RESUME_AFTER_RESTART)
        )

    async def _recheck_kept(self) -> None:
        kept = tuple(h for h in self.fg.handed if h.key in self._kept)
        self._kept = set()
        self.fg = self.fg.withdraw(frozenset(h.key for h in kept))
        plan = await reconcile_handed(self._bridge, self._agent_id, kept, where="after_resume")
        if plan is None:
            _ = await mark_unconfirmed(self._ctx, self._agent_id, self._projector, kept)
            return
        _ = await self._apply(plan)
        await defer_reconciliation(self._ctx, self._agent_id, plan.keep)

    # meiva:note
    # The transcript path's counterpart of `_adopt_claimed`, run before anything is delivered: the rows an earlier
    # process handed to the CLI and left claimed are registered with the timeline writer as the inputs they were,
    # then the writer reads the record. An input the record shows is settled by the writer (the same transaction
    # that writes its timeline entries), so nothing is decided here for it except the note that the turn it was in
    # died with the process. An input the record does not show was never taken by the CLI: it goes back to the
    # queue and is delivered again in its original order. A session with no record yet has nothing to show, so
    # all of them go back. Reading starts only after they are registered; an error leaves the rows claimed and the
    # writer waiting, for the next invocation.
    # meiva:link agent/graph/docs/harness-foreground.md
    async def _adopt_by_record(
        self, timeline: TimelineSession, rows: Sequence[ClaimedInbound]
    ) -> None:
        grouped = self._grouped_with_text(rows)
        for handed, text in grouped:
            self._register(timeline, handed, text)
        timeline.start_reading()
        if not grouped:
            return
        while await timeline.read_now():
            pass
        shown = {sent.key for sent in timeline.undelivered()}
        missing = [handed for handed, _ in grouped if handed.key in shown]
        self._forget(missing)
        await requeue_handed(self._ctx, self._agent_id, missing)
        if missing:
            self._changed.set()
        self._queue_resume_note([handed for handed, _ in grouped if handed.key not in shown])

    # meiva:note
    # The transcript path's counterpart of reconciling after a failure: the session is retired first, so a CLI
    # that still held an input in a queue of its own can no longer take it; then the record is read once more
    # and the inputs it shows are settled by the writer. An input it does not show goes back to the queue. A
    # record that cannot be read leaves them claimed for the next invocation. Nothing is consumed with a
    # marker: the record decides, or the input waits.
    async def _retire_by_record(
        self, timeline: TimelineSession, unsettled: Sequence[Handed]
    ) -> list[int]:
        try:
            await self._bridge.retire(reason="inputs handed to a failed run need settling")
            while await timeline.read_now():
                pass
        except Exception:
            logger.opt(exception=True).warning(
                "could not read the harness's record; the inputs stay claimed to be settled by "
                "the next invocation",
                event="harness_reconcile_failed",
                agent_id=self._agent_id,
                keys=[h.key for h in unsettled],
                where="after_failure",
            )
            return []
        shown = {sent.key for sent in timeline.undelivered()}
        missing = [h for h in unsettled if h.key in shown]
        self._forget(missing)
        await requeue_handed(self._ctx, self._agent_id, missing)
        return []

    # meiva:note
    # The one rule of delivery on the transcript path, applied to an input the event stream reports as refused or
    # lost: the record is read once more, and the input has been delivered if the record shows it (the writer has
    # then settled it in the transaction that wrote its entry). An input the record does not show goes back to the
    # queue. A record that cannot be read fails the invocation, and the inputs are then decided by
    # `retire_unconfirmed` the same way. On the event path there is no record, and the answer is no.
    async def _record_shows(self, handed: Handed) -> bool:
        timeline = self._timeline
        if timeline is None:
            return False
        while await timeline.read_now():
            pass
        return handed.key not in {sent.key for sent in timeline.undelivered()}

    def _register_handed(self, handed: Sequence[Handed], inputs: Sequence[HarnessInput]) -> None:
        if self._timeline is None:
            return
        for entry, wake_input in zip(handed, inputs, strict=True):
            self._register(self._timeline, entry, wake_input.text)

    def _register(self, timeline: TimelineSession, handed: Handed, text: str) -> None:
        refs = [f"{handed.key}:{index}" for index in range(len(handed.items))]
        timeline.register_input(
            handed.key,
            text,
            list(zip(refs, handed.items, strict=True)),
            handed.row_ids,
        )
        self._sent_keys.add(handed.key)

    def _forget(self, handed: Sequence[Handed]) -> None:
        if self._timeline is None:
            return
        for entry in handed:
            self._timeline.forget_sent(entry.key)
            self._sent_keys.discard(entry.key)

    # meiva:note
    # The invocation may not end while an input it handed over is not yet shown by the record: the writer settles
    # it when it reads the row, and an invocation that ended first would leave the row claimed for the next one.
    # The wait is bounded by `TRANSCRIPT_END_CEILING_SECONDS` in all; past it the input stays claimed, the writer
    # settles it when the row appears, and the next invocation adopts it if it never does. A writer that gave up
    # fails the invocation as a setup failure.
    async def _wait_for_record(self) -> bool:
        timeline = self._timeline
        if timeline is None:
            return False
        if (fault := timeline.fault) is not None:
            raise HarnessSetupError(f"the transcript timeline writer failed: {fault}") from fault
        if not self._sent_keys.intersection(sent.key for sent in timeline.undelivered()):
            return False
        if self._record_waited >= TRANSCRIPT_END_CEILING_SECONDS:
            logger.warning(
                "the transcript did not show an input the harness took; it stays claimed",
                event="harness_timeline_input_unproven",
                agent_id=self._agent_id,
                keys=sorted(self._sent_keys),
            )
            self._sent_keys.clear()
            return False
        clock = self._ctx.require_clock()
        started = clock.monotonic()
        _ = await timeline.read_now()
        await asyncio.sleep(0.1)
        self._record_waited += clock.monotonic() - started
        return True

    async def execute(self) -> None:
        subscription = await self._bridge.subscribe()
        self._subscription = subscription
        self.fg = replace(self.fg, generation=self._bridge.generation)
        outcome: BaseException | None = None
        try:
            async with asyncio.TaskGroup() as tasks:
                reader = tasks.create_task(
                    self._read(subscription), name=f"harness-read-{self._agent_id}"
                )
                waker = tasks.create_task(self._wake(), name=f"harness-wake-{self._agent_id}")
                try:
                    await self._adopt_claimed()
                    await self._deliver_until_quiet()
                except BaseException as error:
                    outcome = error
                finally:
                    _ = reader.cancel()
                    _ = waker.cancel()
        finally:
            subscription.close()
        if outcome is not None:
            raise outcome
        if self._failure is not None:
            raise self._failure

    async def _read(self, subscription: EventSubscription) -> None:
        try:
            while (event := await subscription.get()) is not None:
                self._reading = True
                try:
                    await self._on_event(event)
                finally:
                    self._reading = False
            self._failure = HarnessClosedError(
                "harness event stream ended while inputs were in flight"
            )
        except Exception as exc:
            self._failure = exc
        self._changed.set()

    async def _wake(self) -> None:
        while True:
            await self._bridge.wait_inbound(DELIVERY_FALLBACK_SECONDS)
            self._changed.set()

    async def _on_event(self, event: BridgeEvent) -> None:
        if self.fg.is_stale(event):
            logger.info(
                "dropped a frame of an earlier harness process",
                event="harness_event_stale",
                agent_id=self._agent_id,
                event_kind=type(event).__name__,
                event_generation=event_generation(event),
                generation=self.fg.generation,
            )
            return
        if (fault := fault_of_event(self._ctx.harness, event)) is not None:
            raise fault
        match event:
            case AssistantText(turn_id=turn_id, text=text):
                self._streamed.setdefault(turn_id, []).append(text)
                await self._projector.on_event(event)
            case (
                ReasoningDelta()
                | ReasoningText()
                | TextDelta()
                | SubagentText()
                | ToolUse()
                | ToolResult()
            ):
                await self._projector.on_event(event)
            case TurnCompleted():
                await self._on_turn_completed(event)
            case _:
                pass
        self.fg, settlements = self.fg.on_event(event)
        self._settling.extend(s.handed for s in settlements)
        for settlement in settlements:
            await self._settle(settlement)
            self._settling.remove(settlement.handed)
        self._changed.set()

    async def _on_turn_completed(self, event: TurnCompleted) -> None:
        turn = completed_turn(event, "".join(self._streamed.pop(event.turn_id, [])))
        if self._timeline is None:
            log_turn_usage(self._ctx, self._agent_id, self._bridge.model, turn)
        await self._projector.finish((turn,), interrupted=turn.interrupted)
        self._turn_ended = True
        if turn.interrupted:
            self.interrupted = True
            return
        self._publisher.emit(LLMDone(agent_id=self._agent_id).model_dump_json())
        await persist_last_active(self._ctx, self._agent_id, turn.text)

    async def _settle(self, settlement: Settlement) -> None:
        match settlement:
            case Commit(handed=handed) if self._timeline is not None:
                if handed.human:
                    self._projector.mark_human()
            case Commit(handed=handed):
                items = await self._projector.append_inbound(
                    lambda: commit_handed(self._ctx, self._agent_id, [handed]),
                    human_input=handed.human,
                )
                publish_inbound(self._ctx, self._agent_id, items)
            case Requeue(handed=handed, reason=reason) if await self._record_shows(handed):
                logger.info(
                    "the record shows an input the harness reported as not taken; it is settled, not sent again",
                    event="harness_input_proven_by_record",
                    agent_id=self._agent_id,
                    key=handed.key,
                    reason=reason,
                )
                self._forget([handed])
                _ = self._folded.pop(handed.key, None)
            case Requeue(handed=handed, reason=reason):
                logger.info(
                    "harness input returned to the queue",
                    event="harness_input_requeued",
                    agent_id=self._agent_id,
                    key=handed.key,
                    reason=reason,
                )
                self._backoff_until = asyncio.get_running_loop().time() + REDELIVER_BACKOFF_SECONDS
                self._forget([handed])
                if handed.inbound_id is None:
                    self._synthetic.insert(0, (handed, self._prompts[handed.key]))
                else:
                    await requeue_handed(self._ctx, self._agent_id, [handed])
                self._restore_folded([handed])

    async def _deliver_until_quiet(self) -> None:
        while self._failure is None:
            self._changed.clear()
            try:
                delivered = await self._deliver_ready()
            except PoolTimeout as exc:
                # meiva:note
                # PoolTimeout comes from borrowing a connection, before any reservation SQL ran, so there
                # is nothing to undo: the same attempt is made after a short wait.
                # meiva:link agent/graph/docs/harness-foreground.md
                logger.warning(
                    "harness delivery could not borrow a database connection",
                    event="harness_delivery_pool_timeout",
                    agent_id=self._agent_id,
                    error=str(exc),
                    retry_after_seconds=POOL_RETRY_SECONDS,
                )
                self._backoff_until = asyncio.get_running_loop().time() + POOL_RETRY_SECONDS
                continue
            if delivered:
                self._resumed = self._resumed or bool(self._kept)
                continue
            if self._ready_to_recheck():
                await self._recheck_kept()
                continue
            backoff = self._backoff_until - asyncio.get_running_loop().time()
            if self._nothing_left() and backoff <= 0:
                if await self._wait_for_record():
                    continue
                if not await self._queue_correction():
                    return
                continue
            timeout = backoff if backoff > 0 else DELIVERY_FALLBACK_SECONDS
            with suppress(TimeoutError):
                await asyncio.wait_for(self._changed.wait(), timeout)

    # meiva:note
    # The invocation may end only when nothing is in flight: no input handed over and unsettled, no turn
    # running, no event read but not yet written, and none still waiting to be read. A turn the CLI began
    # on its own while the agent was idle reaches the invocation as events already queued on its
    # subscription, so an empty `fg` alone does not mean there is nothing to present.
    # meiva:link agent/graph/docs/harness-foreground.md
    def _nothing_left(self) -> bool:
        waiting = self._subscription is not None and self._subscription.unread
        return (
            self.fg.quiet
            and not self._settling
            and not self._synthetic
            and not self._reading
            and not waiting
        )

    # meiva:note
    # The session could not take the batch, so none of it was handed over: rows go back to the queue and
    # synthetic inputs back to the front of the pending ones, to be delivered when a session can.
    # meiva:link agent/graph/docs/harness-foreground.md
    async def _take_back(self, handed: Sequence[Handed]) -> None:
        self.fg = self.fg.withdraw(frozenset(h.key for h in handed))
        self._forget(handed)
        await requeue_handed(self._ctx, self._agent_id, handed)
        self._synthetic[:0] = [(h, self._prompts[h.key]) for h in handed if h.inbound_id is None]
        self._restore_folded(handed)

    # meiva:note
    # A refused, lost or untaken wake input is handed over again with what rode in it: the synthetic
    # inputs go back in front of the pending ones and fold into the next wake input the same way.
    def _restore_folded(self, handed: Sequence[Handed]) -> None:
        for entry in reversed(handed):
            self._synthetic[:0] = self._folded.pop(entry.key, ())

    # meiva:note
    # Reserve the head of the inbox, unless a refusal is still backing off. A refusal can be folded in
    # while the reservation runs; what was just reserved is then handed back unsent, so the refused
    # inputs keep their place ahead of anything newer.
    # meiva:link agent/graph/docs/harness-turn.md
    async def _reserve(self) -> list[ClaimedInbound] | None:
        rows = await reserve_head_batch(self._ctx, self._agent_id)
        if asyncio.get_running_loop().time() < self._backoff_until:
            await self._give_back(rows)
            return None
        return rows

    async def _give_back(self, rows: Sequence[ClaimedInbound]) -> None:
        await requeue_handed(
            self._ctx, self._agent_id, [Handed(f"inbound-{r.id}", r.id, ()) for r in rows]
        )

    def _handed_of(self, item: ClaimedInbound) -> tuple[Handed, str]:
        messages = prepare_steered_inbound(self._ctx, item)
        text = render_messages(messages)
        images, skipped = native_images(messages)
        if skipped:
            text = f"{text}\n\n{skipped_images_note(skipped)}"
        items = timeline_items_of(item, messages, self._ctx)
        entry = Handed(f"inbound-{item.id}", item.id, tuple(items), human=is_human_wake_item(item))
        self._images[entry.key] = images
        return entry, text

    async def _render(self, rows: Sequence[ClaimedInbound]) -> list[tuple[Handed, str]]:
        try:
            return [self._handed_of(item) for item in rows]
        except Exception:
            await self._give_back(rows)
            raise

    # meiva:note
    # After the CLI compacted its own context, the next input also lists the agent's unanswered questions
    # (read from the database). An external CLI has no moment before its compaction, so this is the only
    # place they are shown to it; an agent with none gets no text.
    # meiva:link base/lm/harness/docs/harness.md
    async def _compaction_note(self) -> str:
        if not self._bridge.context_compacted:
            return ""
        return reinjection_text(
            await read_agent_open_questions(self._ctx, self._agent_id),
            source="read from the database, not from your summary",
        )

    async def _deliver_ready(self) -> bool:
        if asyncio.get_running_loop().time() < self._backoff_until:
            return False
        reserved = await self._reserve()
        if reserved is None:
            return False
        rows = recovery_notes_first(reserved)
        synthetic, self._synthetic = self._synthetic, []
        rendered = await self._render(rows)
        plan = plan_wake(
            synthetic,
            [
                (entry, text, is_passive(row))
                for row, (entry, text) in zip(rows, rendered, strict=True)
            ],
            self._images,
            hook_notes_key=HOOK_NOTES_KEY,
        )
        handed, inputs, settled_now, group = (
            list(plan.handed),
            list(plan.inputs),
            list(plan.settled_now),
            plan.group,
        )
        self._prompts.update(plan.prompts)
        if plan.folded:
            self._folded[plan.handed[0].key] = plan.folded
        if not handed and not settled_now:
            return False
        if settled_now:
            items = await self._projector.append_inbound(
                lambda: commit_handed(self._ctx, self._agent_id, settled_now)
            )
            publish_inbound(self._ctx, self._agent_id, items)
        await requeue_handed(self._ctx, self._agent_id, plan.held)
        if not handed:
            return True
        compaction_note = await self._compaction_note()
        self.last_key = handed[-1].key
        self.fg = self.fg.hand(tuple(handed))
        if group is not None:
            await mark_input_group(self._ctx, self._agent_id, group.row_ids, group.key)
        self._register_handed(handed, inputs)
        try:
            await self._bridge.deliver(inputs, compaction_note=compaction_note)
        except HarnessNotDeliveredError:
            await self._take_back(handed)
            raise
        except (HarnessClosedError, HarnessSetupError, ModelInterruptedError):
            raise
        except Exception as exc:
            raise HarnessSetupError(
                f"{self._ctx.harness.value} could not take the input: {exc}"
            ) from exc
        return True

    # meiva:note
    # What the harness stored itself is waited for only while there is something that makes it deliver:
    # once the recovery turn is over and every other input is settled, what is still missing is asked
    # about once more instead of waited for.
    def _ready_to_recheck(self) -> bool:
        return (
            any(h.key in self._kept for h in self.fg.handed)
            and self._resumed
            and not self._synthetic
            and not self._settling
            and not self.fg.turns
            and all(h.key in self._kept for h in self.fg.handed)
        )

    async def _queue_correction(self) -> bool:
        if not self._turn_ended or self.interrupted:
            return False
        self._turn_ended = False
        invalid = self._projector.take_invalid_reply()
        correction = await harness_reply_correction(
            self._ctx,
            self._agent_id,
            self._baseline,
            invalid=invalid,
            key=self.last_key,
            correcting=self._correcting,
        )
        if correction is None:
            return False
        self._correcting = True
        self._synthetic.append(correction)
        return True


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
