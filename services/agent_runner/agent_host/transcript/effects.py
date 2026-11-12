from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_activity import resolve_pricing_model
from agent.graph.claim.harness_inbox import Emits, publish_items
from agent.graph.hooks.user_delivery import capture_items
from agent.harness_timeline.store import aconfirm_replies, aload_unconfirmed_replies
from agent.harness_timeline.writer import Committed
from base.agents.context import MeivaContext
from base.agents.history.timeline_item import TimelineItem
from base.agents.to_user.delivery.store import DeliveryConflictError
from base.clock import Clock
from base.lm.harness.session.transcript.drafts import MessageUsage
from base.lm.harness.session.transcript.entries import MAIN_RECORD
from base.lm.harness.session.transcript.projection import RecentSet
from base.lm.usage import UsageFields, log_usage_fields
from base.log import logger

# meiva:note
# What happens after the timeline writer commits a read, for the agent host to run beside it: the items that
# were written are published to the live view, the replies meant for the person are captured for delivery, each
# reliable model message's spend is logged as the same `llm_usage` event the built-in loop logs, and what the
# parser could not use or could not place is reported. None of it is part of the transaction, and none of it can
# fail the commit or another effect: each runs on its own, a failure is logged as `harness_timeline_effect_failed`
# and what failed is kept to be tried again at the next commit, so one effect failing never costs another.
#
# Publishing goes through a publisher of the runtime (`TimelineDeps.publisher_factory`), not the graph
# invocation's: a row read after the invocation ended (a background sub-agent's output) reaches the live view too.
# The spend is kept as "owed" (bounded, in memory) until it has been logged once per `(record, message id)`. The
# replies are not kept in memory at all: which replies the writer committed and nobody has handed to delivery is
# read from the database, as the keyed `agent_chat` rows after the agent's reply progress
# (`agent_harness_timeline_progress.replies_seq`). The writer makes that row, in the transaction of its first commit,
# at the end of the timeline as it was before the commit, so a fork's copied history, an agent's earlier event-path
# history and replies written while the delivery mode was `sdk` (which hands nothing over) are never offered; the
# effects raise it only after the replies up to it have been through `capture_items`, which publishes under
# `harness:<item id>` and is unique by that key. A process that dies between a commit and its effects loses the
# spend of that read (a log, not a table: nothing can tell what was logged), but not the replies: the same pass runs
# after every commit and when the writer starts (`recover`), and reads exactly the part of the timeline between the
# progress and its end, however long the timeline is. Replies are handed over one at a time: one that conflicts with
# a delivery already recorded (the user changed the delivery mode) is dropped with `harness_timeline_reply_dropped`
# and does not hold the ones after it; any other failure leaves it and the ones after it for the next pass. Every
# pass, the recovery included, runs under one lock with the pricing, so two of them never interleave.
#
# A message the CLI cut short (an interrupt) has only a placeholder count in the record, so it is not priced: the
# accounting misses what such a message cost, which is a known gap, and `harness_timeline_usage_unreliable` counts
# those messages (one event each, per record and message) so that the gap is a number. A model the catalog does not
# price is warned about once per runtime, not once per message (the usage rows themselves are all written, unpriced).
#
# Usage is logged when the entry is read (never back-filled into a turn that was settled), once per
# `message.id`, for every reliable message: the main record's as `usage_kind="agent"` and a sub-agent's as
# `usage_kind="agent_subagent"` with `parent_call_id` naming the call that started it when known, priced on the
# message's own model. This counts the sub-agents' tokens, which the turn result's usage excludes, so a
# transcript-sourced agent's logged usage is higher than the adapter-event path's by exactly that spend.
#
# The context is the latest hosted turn's, bound by the host (`bind`): between turns it still carries the
# database, bus and clock handles the capture and the pricing use.
# meiva:link agent/harness_timeline/README.md

MAIN_USAGE_KIND = "agent"
SUBAGENT_USAGE_KIND = "agent_subagent"
OWED_LIMIT = 256
LOGGED_LIMIT = 8_192
MODELS_LIMIT = 256
UNMATCHED_LOG_FIRST = 20
UNMATCHED_LOG_EVERY = 100
FAILURE_LOG_SECONDS = 60.0


async def capture_replies(ctx: MeivaContext, agent_id: int, items: list[TimelineItem]) -> None:
    await capture_items(ctx, agent_id, items, source_key="harness")


class TimelineEffects:
    def __init__(
        self,
        agent_id: int,
        publisher: Emits,
        *,
        pool: AsyncConnectionPool,
        clock: Clock,
        capture: Callable[
            [MeivaContext, int, list[TimelineItem]], Awaitable[None]
        ] = capture_replies,
    ) -> None:
        self._agent_id = agent_id
        self._publisher = publisher
        self._pool = pool
        self._clock = clock
        self._capture = capture
        self._ctx: MeivaContext | None = None
        self._settle = asyncio.Lock()
        self._owed_usage: deque[MessageUsage] = deque(maxlen=OWED_LIMIT)
        self._logged: RecentSet[tuple[str, str]] = RecentSet(LOGGED_LIMIT)
        self._unmatched = 0
        self._models: RecentSet[str] = RecentSet(MODELS_LIMIT)
        self._unreliable: RecentSet[tuple[str, str]] = RecentSet(LOGGED_LIMIT)
        self._failures: dict[str, _FailureLog] = {}

    def bind(self, ctx: MeivaContext) -> None:
        self._ctx = ctx

    async def recover(self) -> None:
        await self.settle_owed()

    async def after_commit(self, commit: Committed) -> None:
        self._report(commit)
        written = list(commit.written)
        await self._isolated("publish", lambda: self._publish(written))
        self._owed_usage.extend(commit.plan.usage)
        await self.settle_owed()

    async def settle_owed(self) -> None:
        async with self._settle:
            await self._isolated("capture", self._capture_owed)
            await self._isolated("usage", self._price_owed)

    async def _publish(self, written: list[TimelineItem]) -> None:
        publish_items(self._publisher, self._agent_id, written)

    async def _capture_owed(self) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        while True:
            async with self._pool.connection() as conn:
                owed = await aload_unconfirmed_replies(conn, self._agent_id, limit=OWED_LIMIT)
            if owed is None:
                return
            for seq, item in owed.replies:
                try:
                    await self._capture(ctx, self._agent_id, [item])
                except DeliveryConflictError as error:
                    logger.warning(
                        "a reply conflicts with a delivery already recorded and is dropped",
                        event="harness_timeline_reply_dropped",
                        agent_id=self._agent_id,
                        item_id=item.item_id,
                        error=str(error),
                    )
                except Exception:
                    await self._confirm(seq - 1)
                    raise
            await self._confirm(owed.reach)
            if not owed.full:
                return

    async def _confirm(self, seq: int) -> None:
        async with self._pool.connection() as conn:
            await aconfirm_replies(conn, self._agent_id, seq)

    async def _price_owed(self) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        while self._owed_usage:
            spend = self._owed_usage.popleft()
            ident = (spend.record, spend.message_ref)
            if ident in self._logged:
                continue
            try:
                self._log_usage(ctx, spend)
            except Exception as error:
                self._owed_usage.append(spend)
                logger.warning(
                    "a message's usage could not be priced; it is tried again at the next commit",
                    event="harness_usage_log_failed",
                    agent_id=self._agent_id,
                    error=repr(error),
                    message_ref=spend.message_ref,
                )
                return
            self._logged.add(ident)

    def _report(self, commit: Committed) -> None:
        for report in commit.plan.anomalies:
            logger.warning(
                "the transcript held a row the projection could not use",
                event="harness_timeline_anomaly",
                agent_id=self._agent_id,
                kind=report.anomaly.value,
                detail=report.detail,
            )
        for key in commit.plan.unmatched_inputs:
            self._unmatched += 1
            if self._unmatched <= UNMATCHED_LOG_FIRST or self._unmatched % UNMATCHED_LOG_EVERY == 1:
                logger.warning(
                    "the transcript showed an input this process did not send",
                    event="harness_timeline_unmatched_input",
                    agent_id=self._agent_id,
                    entry_key=key,
                    seen_in_this_session=self._unmatched,
                )
        for spend in commit.plan.unreliable_usage:
            ident = (spend.record, spend.message_ref)
            if ident in self._unreliable:
                continue
            self._unreliable.add(ident)
            logger.info(
                "a model message was cut short and its spend is not in the accounting",
                event="harness_timeline_usage_unreliable",
                agent_id=self._agent_id,
                record=spend.record,
                message_ref=spend.message_ref,
                turn=spend.turn,
                model=spend.model,
            )

    def _log_usage(self, ctx: MeivaContext, spend: MessageUsage) -> None:
        usage = spend.usage
        if usage.input_tokens == 0 and usage.output_tokens == 0:
            return
        catalog = ctx.require_catalog()
        model = resolve_pricing_model(spend.model or ctx.require_agent().brain.llm_model, catalog)
        sub = spend.record != MAIN_RECORD
        known = model in self._models
        self._models.add(model)
        _ = log_usage_fields(
            model,
            UsageFields(
                tok_in=usage.input_tokens,
                tok_out=usage.output_tokens,
                tok_cached=usage.cache_read_input_tokens or None,
                cache_write_5m=usage.cache_creation_input_tokens,
                source=ctx.harness.value,
                for_agent_id=self._agent_id,
                calls=1,
                parent_call_id=spend.parent_call_id,
                unpriced_already_reported=known,
            ),
            usage_kind=SUBAGENT_USAGE_KIND if sub else MAIN_USAGE_KIND,
            catalog=catalog,
            now=ctx.require_clock().now(),
        )

    # meiva:note
    # One effect on its own: a failure is logged and kept apart from the others. A failure that repeats is not a flood:
    # the first is logged with its stack, the same effect's next ones are counted and reported at most once per
    # `FAILURE_LOG_SECONDS` with the number since, and a success starts the count again.
    async def _isolated(self, effect: str, run: Callable[[], Awaitable[None]]) -> None:
        try:
            await run()
        except Exception:
            now = self._clock.monotonic()
            seen = self._failures.get(effect)
            if seen is not None and now - seen.logged_at < FAILURE_LOG_SECONDS:
                seen.suppressed += 1
                return
            self._failures[effect] = _FailureLog(now)
            logger.opt(exception=True).warning(
                "an effect of a committed read failed; what it owed is kept and tried again at the "
                "next commit",
                event="harness_timeline_effect_failed",
                agent_id=self._agent_id,
                effect=effect,
                failures_since_last_report=0 if seen is None else seen.suppressed,
            )
        else:
            _ = self._failures.pop(effect, None)


@dataclass(slots=True)
class _FailureLog:
    logged_at: float
    suppressed: int = 0
