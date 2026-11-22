from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import replace
from typing import Literal

from psycopg_pool import PoolTimeout

from agent import state as _state
from agent.db import ClaimedInbound
from agent.graph.claim._dispatch import is_human_wake_item, prepare_steered_inbound
from agent.graph.claim.harness_activity import log_turn_usage
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
    apply_reconciled,
    claimed_deliverable,
    commit_handed,
    defer_reconciliation,
    harness_reply_correction,
    is_deferred,
    is_passive,
    mark_input_group,
    mark_unconfirmed,
    reconcile_handed,
    recovery_notes_first,
    requeue_handed,
    reserve_head_batch,
    settle_halted_turn,
)
from agent.graph.claim.harness_read_sink import ReadTimelineSink
from agent.graph.claim.harness_render import (
    native_images,
    render_messages,
    skipped_images_note,
)
from agent.graph.claim.turn_stamp import persist_last_active
from agent.graph.hooks.node import HOOK_NOTES_KEY
from agent.graph.hooks.question_context import read_agent_open_questions
from agent.graph.interrupt import (
    ModelInterruptedError,
)
from base.agents.context import MeivaContext
from base.agents.messages.harness_input_key import recorded_input_key, row_input_key
from base.agents.questions.render import reinjection_text
from base.events.live.projection import LLMDone
from base.events.live.publisher import AgentEventPublisher
from base.lm.harness import (
    AssistantText,
    BackgroundWaitReporting,
    BridgeEvent,
    EventSubscription,
    HarnessClosedError,
    HarnessNotDeliveredError,
    HarnessSetupError,
    HarnessTurnStalledError,
    ImageBlock,
    InputInContext,
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

# meiva:note
# Delivery is woken by the host's inbound wake, so the queue is not polled while a turn runs. The
# fallback only bounds the cost of a lost wake. After a refusal (Antigravity refuses an input for a
# conversation that is just starting or just ending) the same input is not sent again by this run.
# The run waits for the foreground and a short backoff, then the next invocation owns the retry.
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
        projector: ReadTimelineSink,
        state: _state.AgentState,
        publisher: AgentEventPublisher,
        *,
        stall_seconds: float,
    ) -> None:
        if stall_seconds <= 0:
            raise ValueError(f"stall_seconds must be positive, got {stall_seconds!r}")
        self._ctx = ctx
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
        self._inline_image_text: dict[str, str] = {}
        for raw in state.harness_inputs:
            key, prompt = str(raw["key"]), str(raw["prompt"])
            if not prompt.strip():
                raise ValueError(f"synthetic input {key!r} has no text")
            self._synthetic.append((Handed(key, None), prompt))
        self.last_key = f"inbound-turn-{agent_id}"
        self.interrupted = False
        self._correcting = False
        self._turn_ended = False
        self._streamed: dict[str, list[str]] = {}
        self._turn_inputs: dict[str, list[str]] = {}
        self._committed_by_turn: dict[str, list[Handed]] = {}
        self._taken_back_for_abort: tuple[Handed, ...] = ()
        self._changed = asyncio.Event()
        self._backoff_until = 0.0
        self._handoff_after_requeue = False
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
        self._clock = ctx.require_clock()
        self._stall_seconds = stall_seconds
        self._last_heard = 0.0
        self._confirmed_background_wait = False

    def _unsettled(self) -> tuple[Handed, ...]:
        return (*self.fg.handed, *self._settling)

    @property
    def awaiting_reply(self) -> bool:
        return self.fg.awaiting_reply or bool(self._settling)

    @property
    def settled_for_background_exit(self) -> bool:
        return (
            not self._synthetic
            and not self.fg.handed
            and not self._settling
            and (
                (self._confirmed_background_wait and self.fg.can_wait_on_background)
                or (not self.fg.turns and self._turn_ended)
            )
        )

    def pending_inputs(self) -> list[dict[str, str]]:
        return [{"key": handed.key, "prompt": prompt} for handed, prompt in self._synthetic]

    async def settle_halted_turn(self, *, turn_id: str | None, reason: str) -> tuple[int, ...]:
        if turn_id:
            committed = [
                *self._committed_by_turn.pop(turn_id, []),
                *self._committed_by_turn.pop("", []),
            ]
        else:
            committed = [h for items in self._committed_by_turn.values() for h in items]
            self._committed_by_turn.clear()
        taken_back = self._taken_back_for_abort
        self._taken_back_for_abort = ()
        unsettled = self._unsettled()
        to_requeue: list[Handed] = [*committed, *taken_back]
        if unsettled:
            try:
                plan = await self._reconcile_after_failure(unsettled)
            except Exception:
                logger.opt(exception=True).warning(
                    "could not read the harness's record for a halted turn; requeuing inputs",
                    event="harness_reconcile_failed",
                    agent_id=self._agent_id,
                    keys=[h.key for h in unsettled],
                    where="after_halted_turn",
                )
                to_requeue.extend(unsettled)
            else:
                if plan is None:
                    to_requeue.extend(unsettled)
                else:
                    to_requeue.extend(plan.commit)
                    to_requeue.extend(plan.requeue)
                    to_requeue.extend(plan.unknown)
        requeued = await settle_halted_turn(self._ctx, self._agent_id, to_requeue, reason=reason)
        withdrawn_keys = frozenset(h.key for h in to_requeue)
        if withdrawn_keys:
            self.fg = self.fg.withdraw(withdrawn_keys)
            self._settling = [h for h in self._settling if h.key not in withdrawn_keys]
        return requeued

    async def record_halt(self, *, reason: str) -> None:
        _ = await settle_halted_turn(self._ctx, self._agent_id, (), reason=reason)

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
        by_key: dict[str, list[ClaimedInbound]] = {}
        for row in rows:
            by_key.setdefault(recorded_input_key(row.id, row.payload), []).append(row)
        grouped: list[Handed] = []
        for key, group in by_key.items():
            pairs = [(self._handed_of(row), is_passive(row)) for row in group]
            merged = merge_handed([(handed, passive) for (handed, _), passive in pairs])
            grouped.append(replace(merged, key=key))
        return grouped

    def _queue_resume_note(self, settled: Sequence[Handed]) -> None:
        if not settled or self._synthetic:
            return
        newest = max((i for h in settled for i in h.row_ids), default=0)
        self._synthetic.append((Handed(f"reconciled-resume-{newest}", None), RESUME_AFTER_RESTART))

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

    async def execute(self) -> None:
        subscription = await self._bridge.subscribe()
        self._subscription = subscription
        self._last_heard = self._clock.monotonic()
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
                self._last_heard = self._clock.monotonic()
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

    def _advance_foreground(self, event: BridgeEvent) -> tuple[Settlement, ...]:
        self.fg, settlements = self.fg.on_event(event)
        self._settling.extend(s.handed for s in settlements)
        if not self.fg.can_wait_on_background or self.fg.awaiting_reply:
            self._confirmed_background_wait = False
        if isinstance(event, AssistantText) and event.text.strip():
            _ = self._committed_by_turn.pop(event.turn_id, None)
        return settlements

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
        settlements = self._advance_foreground(event)
        match event:
            case AssistantText(turn_id=turn_id, text=text):
                self._streamed.setdefault(turn_id, []).append(text)
                await self._projector.on_event(event)
                if text.strip():
                    await persist_last_active(self._ctx, self._agent_id, text)
            case (
                ReasoningDelta()
                | ReasoningText()
                | TextDelta()
                | SubagentText()
                | ToolUse()
                | ToolResult()
            ):
                await self._projector.on_event(event)
            case InputInContext(key=key, turn_id=turn_id):
                self._turn_inputs.setdefault(turn_id, []).append(key)
            case TurnCompleted():
                await self._on_turn_completed(event)
            case _:
                pass
        for settlement in settlements:
            await self._settle(settlement)
            self._settling.remove(settlement.handed)
        self._changed.set()

    async def _on_turn_completed(self, event: TurnCompleted) -> None:
        turn = completed_turn(event, "".join(self._streamed.pop(event.turn_id, [])))
        log_turn_usage(self._ctx, self._agent_id, self._bridge.model, turn)
        await self._projector.finish(
            (turn,),
            interrupted=turn.interrupted,
            input_keys=self._turn_inputs.pop(event.turn_id, []),
        )
        self._committed_by_turn.pop(event.turn_id, None)
        if not turn.is_error:
            self._projector.reset_turn_delivery()
            self._taken_back_for_abort = ()
        self._turn_ended = True
        if turn.interrupted:
            self.interrupted = True
            return
        self._publisher.emit(LLMDone(agent_id=self._agent_id).model_dump_json())
        await persist_last_active(self._ctx, self._agent_id, turn.text)

    async def _settle(self, settlement: Settlement) -> None:
        match settlement:
            case Commit(handed=handed, turn_id=turn_id):
                self._committed_by_turn.setdefault(turn_id, []).append(handed)
                await self._projector.append_inbound(
                    lambda: commit_handed(self._ctx, self._agent_id, [handed]),
                    human_input=handed.human,
                )
            case Requeue(handed=handed, reason=reason):
                logger.info(
                    "harness input returned to the queue",
                    event="harness_input_requeued",
                    agent_id=self._agent_id,
                    key=handed.key,
                    reason=reason,
                )
                self._backoff_until = asyncio.get_running_loop().time() + REDELIVER_BACKOFF_SECONDS
                self._handoff_after_requeue = True
                if handed.inbound_id is None:
                    self._synthetic.insert(0, (handed, self._prompts[handed.key]))
                else:
                    await requeue_handed(self._ctx, self._agent_id, [handed])
                self._restore_folded([handed])

    async def _deliver_until_quiet(self) -> None:
        while self._failure is None:
            self._changed.clear()
            if self._handoff_after_requeue:
                if await self._settle_before_handoff():
                    return
                continue
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
                if not await self._queue_correction():
                    return
                continue
            await self._wait_for_change(backoff if backoff > 0 else DELIVERY_FALLBACK_SECONDS)

    async def _settle_before_handoff(self) -> bool:
        if self._ready_to_recheck():
            await self._recheck_kept()
            return False
        backoff = self._backoff_until - asyncio.get_running_loop().time()
        if self._foreground_quiet() and backoff <= 0:
            _ = await self._queue_correction()
            return True
        await self._wait_for_change(backoff if backoff > 0 else DELIVERY_FALLBACK_SECONDS)
        return False

    async def _is_waiting_on_background(self) -> bool:
        was_confirmed = self._confirmed_background_wait
        confirmed = False
        if (
            self.fg.can_wait_on_background
            and not self._settling
            and isinstance(self._bridge, BackgroundWaitReporting)
        ):
            try:
                waiting = await self._bridge.waiting_on_background(self.fg.turns)
            except Exception:
                logger.opt(exception=True).warning(
                    "agent {agent_id}: background wait check failed; keeping stall guard armed",
                    agent_id=self._agent_id,
                )
                waiting = False
            confirmed = waiting and self.fg.can_wait_on_background and not self._settling
        self._confirmed_background_wait = confirmed
        if confirmed or was_confirmed:
            self._last_heard = self._clock.monotonic()
        return confirmed

    # meiva:note
    # Every wait of the delivery loop is bounded by `stall_seconds` while inputs or open turns remain
    # in flight, except when every input has delivered a reply (`can_wait_on_background`) AND the live
    # adapter proves the open turn is parked in silent background wait (`BackgroundWaitReporting`, e.g.
    # Antigravity reactive-IDLE with live background tasks). Multi-step turns emitting commentary
    # `AssistantText` before tools remain protected because foreground activity re-arms `awaiting_reply`
    # and adapters only confirm background wait when no planner/foreground step is running. Steering a
    # new input clears `_confirmed_background_wait`, resets `_last_heard`, and starts a fresh delivery
    # wave so prior background silence is never charged to the new input.
    # meiva:link agent/graph/docs/harness-foreground.md
    async def _wait_for_change(self, timeout: float) -> None:
        if not self._foreground_quiet() and not await self._is_waiting_on_background():
            now = self._clock.monotonic()
            if self._reading or (self._subscription is not None and self._subscription.unread):
                self._last_heard = now
            floor = (
                None
                if self.fg.handed or self._settling
                else self._bridge.silence_floor_monotonic(self.fg.turns)
            )
            heard = max(self._last_heard, floor) if floor is not None else self._last_heard
            silent = now - heard
            if silent >= self._stall_seconds:
                raise HarnessTurnStalledError(
                    f"no event from the harness for {silent:.0f}s (threshold "
                    f"{self._stall_seconds:g}s) with "
                    f"{len(self.fg.handed)} input(s) unsettled and {len(self.fg.turns)} turn(s) open",
                    silent_seconds=silent,
                    stall_seconds=self._stall_seconds,
                )
            timeout = min(timeout, self._stall_seconds - silent)
        else:
            now = self._clock.monotonic()
            if self._reading or (self._subscription is not None and self._subscription.unread):
                self._last_heard = now
        with suppress(TimeoutError):
            _ = await asyncio.wait_for(self._changed.wait(), timeout)

    # meiva:note
    # The invocation may end only when nothing is in flight: no input handed over and unsettled, no turn
    # running, no event read but not yet written, and none still waiting to be read. A turn the CLI began
    # on its own while the agent was idle reaches the invocation as events already queued on its
    # subscription, so an empty `fg` alone does not mean there is nothing to present.
    # meiva:link agent/graph/docs/harness-foreground.md
    def _nothing_left(self) -> bool:
        return self._foreground_quiet() and not self._synthetic

    def _foreground_quiet(self) -> bool:
        waiting = self._subscription is not None and self._subscription.unread
        return self.fg.quiet and not self._settling and not self._reading and not waiting

    # meiva:note
    # Inputs known not to have reached the session go back to the queue and synthetic inputs
    # go back to the front of the pending ones, to be delivered when a session can.
    # meiva:link agent/graph/docs/harness-foreground.md
    async def _take_back(self, batch: Sequence[Handed], keys: frozenset[str] | None) -> None:
        handed = [entry for entry in batch if keys is None or entry.key in keys]
        self._taken_back_for_abort = (*self._taken_back_for_abort, *handed)
        self.fg = self.fg.withdraw(frozenset(h.key for h in handed))
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
            self._ctx, self._agent_id, [Handed(row_input_key(r.id, r.payload), r.id) for r in rows]
        )

    def _handed_of(self, item: ClaimedInbound) -> tuple[Handed, str]:
        messages = prepare_steered_inbound(self._ctx, item)
        text = render_messages(messages)
        images, skipped = native_images(messages)
        if skipped:
            text = f"{text}\n\n{skipped_images_note(skipped)}"
        entry = Handed(
            row_input_key(item.id, item.payload), item.id, human=is_human_wake_item(item)
        )
        self._images[entry.key] = images
        self._inline_image_text.pop(entry.key, None)
        if images and not skipped:
            self._inline_image_text[entry.key] = render_messages(messages, inline_images=True)
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

    def _begin_delivery(self, handed: Sequence[Handed]) -> None:
        if self._confirmed_background_wait or not self.awaiting_reply:
            self._projector.reset_turn_delivery()
        self._confirmed_background_wait = False
        self.last_key = handed[-1].key
        self.fg = self.fg.hand(tuple(handed))
        self._last_heard = self._clock.monotonic()

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
            inline_image_text=self._inline_image_text,
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
            await self._projector.append_inbound(
                lambda: commit_handed(self._ctx, self._agent_id, settled_now)
            )
        await requeue_handed(self._ctx, self._agent_id, plan.held)
        if not handed:
            return True
        compaction_note = await self._compaction_note()
        self._begin_delivery(handed)
        if group is not None:
            await mark_input_group(self._ctx, self._agent_id, group.row_ids, group.key)
        self._projector.note_input_keys([item.key for item in inputs])
        try:
            await self._bridge.deliver(inputs, compaction_note=compaction_note)
        except HarnessNotDeliveredError as exc:
            await self._take_back(handed, exc.keys)
            raise
        except (HarnessClosedError, HarnessSetupError, ModelInterruptedError):
            raise
        except Exception as exc:
            raise HarnessSetupError(
                f"{self._ctx.harness.value} could not take the input: {exc}"
            ) from exc
        self._last_heard = self._clock.monotonic()
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
