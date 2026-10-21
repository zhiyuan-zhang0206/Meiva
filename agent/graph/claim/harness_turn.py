from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from typing import Literal

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph.claim.turn_stamp import persist_last_active
from agent.graph.interrupt import (
    ModelInterruptedError,
    interrupt_halt,
    interruptible_model,
    subscribe_interrupt,
)
from agent.graph.llm_errors import FatalLLMStreamError
from agent.nodes import CLAIM
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.messages.kwargs import read_meiva_kwargs
from base.events.live.projection import Cancelled
from base.lm.content import message_content
from base.lm.harness import HarnessClosedError, HarnessSetupError, HarnessTurnFaultedError
from base.lm.harness.engine import HarnessTurn, TurnEngine
from base.lm.usage import log_usage_fields
from base.log import logger

HarnessGoto = Literal["claim"]

# meiva:note
# A harness turn is one long await with no LangGraph step inside it, and the host's
# stall guard reads this process's turn clock rather than the harness's. The node
# therefore marks progress itself while it waits. One minute is a cadence, not a
# threshold: the guard aborts at `MEIVA_HOST_TURN_NO_PROGRESS_TIMEOUT_SECONDS` (40
# minutes), and the adapter's own `turn_timeout_seconds` (15) interrupts a turn
# first, so a tick this slow can only ever keep a live turn alive.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
PROGRESS_TICK_SECONDS = 60.0


# meiva:note
# The messages the harness has not been given yet: everything after the cursor, minus
# the agent's own replies. A reply lands in the same state write as the cursor that
# covers it, so one after the cursor means a native node wrote it (it must not have) —
# either way, feeding the agent's own words back as a user turn is wrong, and the
# cursor skips them.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def pending_messages(state: _state.AgentState) -> list[BaseMessage]:
    cursor = min(max(state.harness_cursor, 0), len(state.messages))
    return [message for message in state.messages[cursor:] if not isinstance(message, AIMessage)]


def render_messages(messages: Sequence[BaseMessage]) -> str:
    return "\n\n".join(render_message(message) for message in messages).strip()


def render_message(message: BaseMessage) -> str:
    content = message_content(message)
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        else:
            parts.append(f"[{block.get('type', 'attachment')}]")
    return "\n".join(parts)


# meiva:note
# The adapter's idempotency key: the newest inbound row in the batch, which is unique
# per claim and stable when the same window is submitted twice (a crashed turn, a
# retry within the session). A window that carries only notes has no row to name, so
# its length stands in; retrying it reuses the same key for the same window.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def turn_key(messages: Sequence[BaseMessage]) -> str:
    for message in reversed(messages):
        inbound_id = read_meiva_kwargs(message).get("meiva_inbound_id")
        if isinstance(inbound_id, int):
            return f"inbound-{inbound_id}"
    return f"window-{len(messages)}"


# meiva:note
# One graph step, one wait: the pending messages go to the harness, and every turn
# completion that comes back — the handoff turn first, on a session that has just
# woken — becomes its own assistant message, so a waking acknowledgement never reads
# as the reply to the first chat. A session that faults or closes under the turn is
# raised as a fatal stream abort: the turn cannot finish, so it settles as a failure
# rather than leaving half a reply, and the next inbound may start a fresh session.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def harness_turn_node(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> Command[HarnessGoto]:
    ctx = runtime.context
    agent_id = agent_id_from_config(config)
    bridge = ctx.require_harness_bridge()
    ctx.turn_progress.mark(agent_id)
    pending = pending_messages(state)
    text = render_messages(pending)
    if not text:
        return Command[HarnessGoto](
            update={"halted": True, "harness_cursor": len(state.messages)}, goto=CLAIM
        )

    try:
        async with subscribe_interrupt(
            ctx.ops_pool,
            agent_id,
            resources=ctx.hosted_resources,
        ) as cancel_event:
            turn = await interruptible_model(
                _ticking(
                    ctx.turn_progress.mark, agent_id, bridge.run_turn(text, key=turn_key(pending))
                ),
                cancel_event,
            )
    except ModelInterruptedError as interrupted:
        _announce_cancel(ctx, agent_id)
        await interrupt_engine(agent_id, bridge)
        return Command[HarnessGoto](update=interrupt_halt(interrupted.reason), goto=CLAIM)
    except (HarnessTurnFaultedError, HarnessClosedError, HarnessSetupError) as exc:
        raise FatalLLMStreamError(f"{ctx.harness.value} harness turn aborted: {exc}") from exc

    turns = (*turn.prelude, turn)
    for completed in turns:
        log_turn_usage(ctx, agent_id, bridge.model, completed)
    # meiva:note
    # The turn's last completion is the agent's last AI message, and it is stamped where
    # the native node stamps it: before this call a harness agent's `last_message_text`
    # stayed NULL for its whole life, so `meiva.agents.get_last_message()` answered
    # `None` for every codex / claude / antigravity agent while its transcript held the
    # text (#5511). The same UPDATE also clears the crash marker and the permanent-reject
    # streak, which a completed harness turn earns exactly as a native one does.
    # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
    await persist_last_active(ctx, agent_id, turns[-1].text)
    return Command[HarnessGoto](
        update={
            "messages": [AIMessage(content=completed.text) for completed in turns],
            "harness_cursor": len(state.messages) + len(turns),
            "halted": True,
        },
        goto=CLAIM,
    )


# meiva:note
# The waited turn is captured and raised after the group closes, the way
# `interruptible_model` does it: a TaskGroup wraps a lone body error in an
# ExceptionGroup, and the node matches a faulted session on the error's own type, so
# the identity has to survive this scope. A cancelled turn keeps its CancelledError
# for the same reason.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def _ticking(
    mark: Callable[[int], None], agent_id: int, turn: Awaitable[HarnessTurn]
) -> HarnessTurn:
    async def settle() -> HarnessTurn | BaseException:
        try:
            return await turn
        except BaseException as error:
            return error

    async def tick() -> None:
        while True:
            await asyncio.sleep(PROGRESS_TICK_SECONDS)
            mark(agent_id)

    async with asyncio.TaskGroup() as tasks:
        ticker = tasks.create_task(tick(), name=f"harness-progress-{agent_id}")
        outcome = await settle()
        ticker.cancel()
        with suppress(asyncio.CancelledError):
            await ticker
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


# meiva:note
# A cancelled turn tells the live view to drop the partial bubble, exactly as an
# interrupted model stream does: the harness turn produced no committed reply, so
# there is nothing else to show. The durable cancel stays a pending inbound row that
# the next claim pass dispatches.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def _announce_cancel(ctx: MeivaContext, agent_id: int) -> None:
    assert ctx.event_publisher is not None, "a cancelled harness turn requires ctx.event_publisher"  # noqa: S101
    ctx.event_publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())


# meiva:note
# An interrupt that cannot be delivered is reported, never raised: the durable cancel
# row is still pending and claim dispatches it on the next pass, while a raise here
# would turn the user's stop into a failed turn. The adapter's `settled` is the one
# honest answer to "did the child processes actually stop", so it is logged as it came
# back — `None` included, which means the CLI tree was not observable rather than reaped.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def interrupt_engine(agent_id: int, bridge: TurnEngine) -> None:
    try:
        report = await bridge.interrupt()
    except Exception as exc:
        logger.warning(
            "[{label}] {body}",
            label="harness",
            event="harness_interrupt_failed",
            body=f"interrupting agent {agent_id}'s harness turn raised: {exc!r}",
        )
        return
    logger.info(
        "[{label}] {body}",
        label="harness",
        event="harness_interrupt",
        body=f"interrupted agent {agent_id}'s harness turn",
        agent_id=agent_id,
        settled=getattr(report, "settled", None),
        turn_id=getattr(report, "turn_id", None),
    )


# meiva:note
# Usage is logged where every other agent turn logs it, with the harness name as the
# `source` so the two runtimes stay comparable. A turn whose provider reported no
# tokens is not logged at all: an all-zero row would read as a free call rather than
# as a provider that said nothing.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def log_turn_usage(ctx: MeivaContext, agent_id: int, model: str | None, turn: HarnessTurn) -> None:
    if turn.replayed:
        return
    usage = turn.usage
    if usage.input_tokens == 0 and usage.output_tokens == 0:
        return
    pricing = (
        model or ctx.require_agent().brain.llm_model,
        ctx.require_catalog(),
    )
    try:
        log_usage_fields(
            pricing[0],
            tok_in=usage.input_tokens,
            tok_out=usage.output_tokens,
            tok_cached=usage.cache_read_input_tokens or None,
            cache_write_5m=usage.cache_creation_input_tokens,
            usage_kind="agent",
            source=ctx.harness.value,
            for_agent_id=agent_id,
            catalog=pricing[1],
        )
    except Exception as error:
        # meiva:note
        # A bill that cannot be priced is a hole in an accounting row; the reply it
        # describes is real either way. The turn has already finished by the time it
        # is logged, so a raise here deletes a reply the user paid for — a provider
        # that reports token counts in a shape the price book rejects must not cost
        # the answer, and the recorded warning keeps the gap visible.
        # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
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
