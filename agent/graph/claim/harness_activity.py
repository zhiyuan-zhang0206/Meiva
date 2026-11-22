from __future__ import annotations

from base.agents.context import MeivaContext
from base.agents.model.harness_models import resolve_harness_catalog_model
from base.lm.catalog import ModelCatalog
from base.lm.harness.engine import HarnessTurn
from base.lm.usage import UsageFields, log_usage_fields
from base.log import logger


# meiva:note
# Usage is logged where every other agent turn logs it, with the harness name as the
# `source` so the two runtimes stay comparable. A turn whose provider reported no
# tokens is not logged at all: an all-zero row would read as a free call rather than
# as a provider that said nothing.
# meiva:link agent/graph/docs/harness-turn.md
def resolve_pricing_model(raw_model: str, catalog: ModelCatalog) -> str:
    return resolve_harness_catalog_model(raw_model, catalog.prices)


def log_turn_usage(ctx: MeivaContext, agent_id: int, model: str | None, turn: HarnessTurn) -> None:
    usage = turn.usage
    if turn.usage_uncertain:
        logger.warning(
            "Incomplete provider usage for agent {agent_id}'s {source} turn {turn_id}",
            event="harness_usage_uncertain",
            agent_id=agent_id,
            source=ctx.harness.value,
            turn_id=turn.turn_id,
            tok_in=usage.input_tokens,
            tok_out=usage.output_tokens,
            tok_cached=usage.cache_read_input_tokens,
            cache_write=usage.cache_creation_input_tokens,
        )
        return
    if usage.input_tokens == 0 and usage.output_tokens == 0:
        return
    catalog = ctx.require_catalog()
    pricing_model = resolve_pricing_model(model or ctx.require_agent().brain.llm_model, catalog)
    entries = (
        [(step, 1) for step in turn.steps if step.input_tokens > 0 or step.output_tokens > 0]
        if turn.steps
        else [(usage, max(turn.num_turns, 1))]
    )
    try:
        for index, (step_usage, calls) in enumerate(entries):
            fields = UsageFields(
                tok_in=step_usage.input_tokens,
                tok_out=step_usage.output_tokens,
                tok_cached=step_usage.cache_read_input_tokens or None,
                cache_write_5m=step_usage.cache_creation_input_tokens,
                source=ctx.harness.value,
                for_agent_id=agent_id,
                calls=calls,
                unpriced_already_reported=index > 0,
            )
            log_usage_fields(
                pricing_model,
                fields,
                usage_kind="agent",
                catalog=catalog,
                now=ctx.require_clock().now(),
            )
    except Exception as error:
        # meiva:note
        # A bill that cannot be priced is a hole in an accounting row; the reply it
        # describes is real either way. The turn has already finished by the time it
        # is logged, so a raise here deletes a reply the user paid for — a provider
        # that reports token counts in a shape the price book rejects must not cost
        # the answer, and the recorded warning keeps the gap visible.
        # meiva:link agent/graph/docs/harness-turn.md
        logger.warning(
            "[{label}] {body}",
            label="harness",
            event="harness_usage_log_failed",
            body=(
                f"agent {agent_id}'s {ctx.harness.value} turn finished but its usage could not be "
                f"priced: {error!r}"
            ),
            agent_id=agent_id,
            tok_in=usage.input_tokens,
            tok_out=usage.output_tokens,
            tok_cached=usage.cache_read_input_tokens,
            cache_write=usage.cache_creation_input_tokens,
        )
