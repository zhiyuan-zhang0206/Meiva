from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future

from base.agents.context import MeivaContext
from base.agents.model.harness_models import resolve_harness_catalog_model
from base.api_contracts.usage import (
    ScheduledUsageReceipt,
    UsageLedgerEntry,
    UsageLedgerWriteError,
    UsageReceiptSubmitter,
)
from base.lm.catalog import ModelCatalog
from base.lm.harness import HarnessTurnFaultedError, Usage
from base.lm.harness.engine import HarnessTurn
from base.lm.pricing.cache_writes import cache_write_tokens, default_write_ttl
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


async def stage_turn_usage(
    ctx: MeivaContext, agent_id: int, model: str | None, turn: HarnessTurn
) -> None:
    receipts = ctx.usage_receipts
    if receipts is None:
        await asyncio.to_thread(log_turn_usage, ctx, agent_id, model, turn)
        return

    staged: list[Future[None]] = []

    def schedule(
        entry: UsageLedgerEntry, write: UsageReceiptSubmitter, observe: Callable[[], None]
    ) -> Future[None]:
        accepted = receipts.schedule(entry, write, observe)
        staged.append(accepted)
        return accepted

    # meiva:link base/lm/usage/README.md
    log_turn_usage(
        ctx, agent_id, model, turn, receipts=ScheduledUsageReceipt(receipts.write, schedule)
    )
    if staged:
        _ = await asyncio.shield(
            asyncio.gather(*(asyncio.wrap_future(job) for job in staged), return_exceptions=True)
        )


async def stage_fault_usage(
    ctx: MeivaContext, agent_id: int, model: str | None, fault: Exception
) -> None:
    if not isinstance(fault, HarnessTurnFaultedError) or fault.outcome is None:
        return
    try:
        if ctx.usage_receipts is None:
            await stage_turn_usage(ctx, agent_id, model, fault.outcome)
        else:
            log_turn_usage(ctx, agent_id, model, fault.outcome, receipts=ctx.usage_receipts)
    except Exception as error:
        logger.warning(
            "Failed to record faulted harness usage ({error_type})",
            event="harness_usage_log_failed",
            error_type=type(error).__name__,
            agent_id=agent_id,
            turn_id=fault.outcome.turn_id,
        )


def log_turn_usage(
    ctx: MeivaContext,
    agent_id: int,
    model: str | None,
    turn: HarnessTurn,
    *,
    receipts: ScheduledUsageReceipt | None = None,
) -> None:
    entries = _reported_units(turn)
    if not entries:
        return
    usage = turn.usage
    if turn.usage_uncertain:
        _warn_incomplete(ctx, agent_id, turn)
    catalog = ctx.require_catalog()
    pricing_model = resolve_pricing_model(model or ctx.require_agent().brain.llm_model, catalog)
    source = ctx.harness.value
    identity = (
        f"harness:{source}:{agent_id}:{turn.session_id or ''}:{turn.generation}:{turn.turn_id}"
    )
    now = ctx.require_clock().now()
    ttl = default_write_ttl(catalog.prices.vendor(pricing_model))
    write_error: UsageLedgerWriteError | None = None
    for index, (step_usage, calls) in enumerate(entries):
        try:
            write_5m, write_1h, write_30m = cache_write_tokens(
                {"cache_creation": step_usage.cache_creation_input_tokens}, default_ttl=ttl
            )
            fields = UsageFields(
                tok_in=step_usage.input_tokens,
                tok_out=step_usage.output_tokens,
                tok_cached=step_usage.cache_read_input_tokens or None,
                cache_write_5m=write_5m,
                cache_write_1h=write_1h,
                cache_write_30m=write_30m,
                source=source,
                for_agent_id=agent_id,
                calls=calls,
                complete=not turn.usage_uncertain,
                call_id=f"{identity}:{index}",
                unpriced_already_reported=index > 0,
            )
            _ = log_usage_fields(
                pricing_model,
                fields,
                usage_kind="agent",
                catalog=catalog,
                now=now,
                submit_receipt=receipts,
            )
        except UsageLedgerWriteError as error:
            if write_error is None:
                write_error = error
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

    if write_error is not None:
        raise write_error


def _reported_units(turn: HarnessTurn) -> list[tuple[Usage, int]]:
    usage = turn.usage
    if turn.usage_uncertain:
        return [(usage, max(turn.num_turns, 1))]
    if turn.steps:
        return [(step, 1) for step in turn.steps if step.input_tokens or step.output_tokens]
    return [(usage, max(turn.num_turns, 1))] if usage.input_tokens or usage.output_tokens else []


def _warn_incomplete(ctx: MeivaContext, agent_id: int, turn: HarnessTurn) -> None:
    usage = turn.usage
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
