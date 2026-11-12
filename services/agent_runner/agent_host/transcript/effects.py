from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable

from agent.graph.claim.harness_activity import resolve_pricing_model
from agent.graph.claim.harness_inbox import Emits, publish_items
from agent.graph.hooks.user_delivery import capture_items
from agent.harness_timeline.writer import Committed
from base.agents.context import MeivaContext
from base.agents.history.timeline_item import TimelineItem
from base.host.boundary.recent import RecentSet
from base.lm.harness.session.transcript.drafts import MessageUsage
from base.lm.harness.session.transcript.entries import MAIN_RECORD
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
# The replies and the spend are kept as "owed" (bounded) until they have gone through, and both are idempotent:
# `capture_items` publishes under `harness:<item id>` and the publication is unique by that key, and a message's
# spend is logged once per `(record, message id)`. A process that dies between a commit and its effects loses the
# spend of that read (a log, not a table: nothing can tell what was logged), but not the replies: `recover`, run when the
# writer starts, offers the latest stored replies to the same idempotent capture again.
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
RECOVER_ROWS = 500


async def capture_replies(ctx: MeivaContext, agent_id: int, items: list[TimelineItem]) -> None:
    await capture_items(ctx, agent_id, items, source_key="harness")


class TimelineEffects:
    def __init__(
        self,
        agent_id: int,
        publisher: Emits,
        *,
        capture: Callable[
            [MeivaContext, int, list[TimelineItem]], Awaitable[None]
        ] = capture_replies,
    ) -> None:
        self._agent_id = agent_id
        self._publisher = publisher
        self._capture = capture
        self._ctx: MeivaContext | None = None
        self._owed_replies: deque[TimelineItem] = deque(maxlen=OWED_LIMIT)
        self._owed_usage: deque[MessageUsage] = deque(maxlen=OWED_LIMIT)
        self._logged: RecentSet[tuple[str, str]] = RecentSet(LOGGED_LIMIT)
        self._unmatched = 0
        self._models: RecentSet[str] = RecentSet(MODELS_LIMIT)
        self._unreliable: RecentSet[tuple[str, str]] = RecentSet(LOGGED_LIMIT)

    def bind(self, ctx: MeivaContext) -> None:
        self._ctx = ctx

    async def recover(self, recent_replies: Callable[[int], Awaitable[list[TimelineItem]]]) -> None:
        await self._isolated(
            "recover", lambda: self._recover(recent_replies), agent_id=self._agent_id
        )

    async def _recover(
        self, recent_replies: Callable[[int], Awaitable[list[TimelineItem]]]
    ) -> None:
        self._owed_replies.extend(await recent_replies(RECOVER_ROWS))
        await self.settle_owed()

    async def after_commit(self, commit: Committed) -> None:
        self._report(commit)
        written = list(commit.written)
        await self._isolated("publish", lambda: self._publish(written), agent_id=self._agent_id)
        self._owed_replies.extend(item for item in written if item.kind == "agent_chat")
        self._owed_usage.extend(commit.plan.usage)
        await self.settle_owed()

    async def settle_owed(self) -> None:
        await self._isolated("capture", self._capture_owed, agent_id=self._agent_id)
        await self._isolated("usage", self._price_owed, agent_id=self._agent_id)

    async def _publish(self, written: list[TimelineItem]) -> None:
        publish_items(self._publisher, self._agent_id, written)

    async def _capture_owed(self) -> None:
        ctx = self._ctx
        if ctx is None or not self._owed_replies:
            return
        items = list(self._owed_replies)
        await self._capture(ctx, self._agent_id, items)
        for _ in items:
            _ = self._owed_replies.popleft()

    async def _price_owed(self) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        for _ in range(len(self._owed_usage)):
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

    async def _isolated(
        self, effect: str, run: Callable[[], Awaitable[None]], *, agent_id: int
    ) -> None:
        try:
            await run()
        except Exception:
            logger.opt(exception=True).warning(
                "an effect of a committed read failed; what it owed is kept and tried again at the "
                "next commit",
                event="harness_timeline_effect_failed",
                agent_id=agent_id,
                effect=effect,
            )
