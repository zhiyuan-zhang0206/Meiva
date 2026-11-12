from __future__ import annotations

from agent.graph.claim.harness_activity import resolve_pricing_model
from agent.graph.claim.harness_inbox import publish_inbound
from agent.graph.hooks.user_delivery import capture_items
from agent.harness_timeline.writer import Committed
from base.agents.context import MeivaContext
from base.lm.harness.session.transcript.drafts import MessageUsage
from base.lm.usage import UsageFields, log_usage_fields
from base.log import logger

# meiva:note
# What happens after the timeline writer commits a read, for the agent host to run beside it: the items that
# were written are published to the live view, the replies meant for the person are captured for delivery, each
# reliable model message's spend is logged as the same `llm_usage` event the built-in loop logs, and what the
# parser could not use or could not place is reported. It is not part of the transaction: a failure here is
# logged and never undoes or repeats a commit.
#
# Usage is logged when the entry is read (never back-filled into a turn that was settled), once per
# `message.id`, for every reliable message: the main record's as `usage_kind="agent"` and a sub-agent's as
# `usage_kind="agent_subagent"` with `parent_call_id` naming the call that started it, priced on the message's
# own model. This counts the sub-agents' tokens, which the turn result's usage excludes, so a transcript-sourced
# agent's logged usage is higher than the adapter-event path's by exactly that spend.
#
# The context is the latest hosted turn's, bound by the host (`bind`): between turns it still carries the
# database, bus and clock handles the effects use.
# meiva:link agent/harness_timeline/README.md

MAIN_USAGE_KIND = "agent"
SUBAGENT_USAGE_KIND = "agent_subagent"


class TimelineEffects:
    def __init__(self, agent_id: int) -> None:
        self._agent_id = agent_id
        self._ctx: MeivaContext | None = None

    def bind(self, ctx: MeivaContext) -> None:
        self._ctx = ctx

    async def after_commit(self, commit: Committed) -> None:
        ctx = self._ctx
        if ctx is None:
            raise RuntimeError("the timeline effects ran before any hosted turn was bound")
        for report in commit.plan.anomalies:
            logger.warning(
                "the transcript held a row the projection could not use",
                event="harness_timeline_anomaly",
                agent_id=self._agent_id,
                kind=report.anomaly.value,
                detail=report.detail,
            )
        for key in commit.plan.unmatched_inputs:
            logger.warning(
                "the transcript showed an input this process did not send",
                event="harness_timeline_unmatched_input",
                agent_id=self._agent_id,
                entry_key=key,
            )
        written = list(commit.written)
        if written:
            publish_inbound(ctx, self._agent_id, written)
            await capture_items(ctx, self._agent_id, written, source_key="harness")
        for spend in commit.plan.usage:
            self._log_usage(ctx, spend)

    def _log_usage(self, ctx: MeivaContext, spend: MessageUsage) -> None:
        usage = spend.usage
        if usage.input_tokens == 0 and usage.output_tokens == 0:
            return
        try:
            catalog = ctx.require_catalog()
            model = resolve_pricing_model(
                spend.model or ctx.require_agent().brain.llm_model, catalog
            )
            sub = spend.parent_call_id is not None
            log_usage_fields(
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
                ),
                usage_kind=SUBAGENT_USAGE_KIND if sub else MAIN_USAGE_KIND,
                catalog=catalog,
                now=ctx.require_clock().now(),
            )
        except Exception as error:
            logger.warning(
                "a message's usage could not be priced; its accounting row is missing",
                event="harness_usage_log_failed",
                agent_id=self._agent_id,
                error=repr(error),
                message_ref=spend.message_ref,
            )
