"""exec node: agent-written code runs in a disposable subprocess.

Each execute_code call runs in a fresh child process (`agent/execution/child.py`),
so a stuck native call is SIGKILLable without touching the agent process
(issue #184). Each graph step executes one call and returns its delta to LangGraph.
Pending calls route back to exec after that commit; the completed batch routes to
after_exec, then claim, which decides whether to wait or continue the turn.

Core mechanisms:
  - Subprocess backend (`agent/graph/exec/_subprocess.py`): the parent spawns
    one `python -I -X utf8 -m agent.execution.child` per exec, polls every 50ms, streams
    output through the chunk pipeline. Cancel/timeout sends a signal then
    closes the process group after a grace period. Natural root exit also closes
    the domain, so an `os._exit`
    cannot strand an ordinary descendant holding stdout. Cancellation returns
    only after the direct child is reaped and the pipe reader gets its bounded
    join. The child rebuilds the state snapshot from the request envelope; the
    state-update delta (plugin fields, security findings) rides the result
    envelope back and is validated here.
  - Halt signal uses exception type rather than exit code: agent code raising
    `LifecycleExit` (AgentTermination / AgentRestart / SystemHalt) → captured
    in result_holder["lifecycle"] → exec_node decides halted + writes marker
    based on isinstance.
  - The child writes stdout/stderr line-buffered onto the pipe (both
    streams merge chronologically — same as what running Python in a
    terminal shows); the parent drains the pipe into a `StreamingTextIO`
    and pushes each new accumulated chunk to redis every 50ms poll
    (frontend streaming). Accumulation is bounded by
    `exec_output_accumulation_max_chars`: past it the middle is dropped as
    it streams and a `StreamCap` rides the result to the envelope, so a
    runaway print loop is truncated rather than left to OOM the parent —
    the run itself is not killed.
  - `run_in_subprocess` returns the `ExecResult` sum type
    (`ExecDone | ExecCancelled | ExecTimedOut | ExecLifecycle |
    ExecCrashed`) plus the raw child envelope; exec_node dispatches via
    `match`, illegal state combinations are unrepresentable. Ordinary
    exception tracebacks are already in the stream output.

State type hint (`state: _state.AgentState`): the annotation is static only, the base schema, so
`state.messages` / `state.halted` type-check; plugin fields are accessed dynamically. The state a
node receives is the class `build_graph` registers it with (`input_schema=`, which carries the
plugins' channels), not what the annotation names.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Sequence
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.messages import AIMessage, AnyMessage, ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command, Overwrite

from agent import state as _state
from agent.execution.protocol import ResultPayload
from agent.graph._attach_drain import build_attach_message
from agent.graph._attach_merge import merge_attachments
from agent.graph.agent_traceback import format_full_traceback
from agent.graph.exec._notes import merge_exec_notes
from agent.graph.interrupt import interrupt_halt, subscribe_interrupt
from agent.graph.node_log import StallConfig, node_lifecycle
from agent.graph.tool_calls import code_from_args, normalize_tool_calls
from agent.messages import exec_output_message
from agent.nodes import AFTER_EXEC, EXEC
from agent.state import AttachState, validate_plugin_state_keys
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.messages.kwargs import ExecStatus
from base.events.live.projection import Cancelled, ExecOutput, ExecStart
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.log import logger

from ._crop import CropLimits
from ._dispatch import CrashFacts, DispatchKind, DispatchPlan, plan_dispatch
from ._result import (
    ExecCancelled as ExecCancelled,
)
from ._result import (
    ExecChildError as ExecChildError,
)
from ._result import (
    ExecCrashed as ExecCrashed,
)
from ._result import (
    ExecDone as ExecDone,
)
from ._result import (
    ExecLifecycle as ExecLifecycle,
)
from ._result import (
    ExecResult as ExecResult,
)
from ._result import (
    ExecTimedOut as ExecTimedOut,
)
from ._stream import (
    ExecOutputChunkPublisher as ExecOutputChunkPublisher,
)
from ._stream import (
    StreamingTextIO as StreamingTextIO,
)
from .output import ExecEnvelope, OutputLimits, overflow_dir, wrap_code_output
from .process import ExecProcess

# Each exec step commits one call; remaining calls return to EXEC before AFTER_EXEC.
ExecGoto = Literal["exec", "after_exec"]

# The `ExecResult` sum type — 5 mutually exclusive variants + a shared output
# field — lives in `_result.py` (moved there so the exec-subprocess
# machinery can construct the same type without importing this module, which
# would close an import cycle). Re-exported here: exec_node's match dispatch
# and existing tests keep importing from agent.graph.exec.node.
#
# Lifecycle priority is implemented at the construction site
# (`_construct_exec_result`): if a lifecycle exc exists, `ExecLifecycle` is
# constructed directly, skipping the cancelled/timed_out branches — the
# "lifecycle always wins" race decision moved from dispatch site to
# construction site; exec_node match no longer has to consider the race.


async def exec_node(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
    *,
    process: ExecProcess,
) -> Command[ExecGoto]:
    """Run agent-written code in one disposable child process. See module docstring."""
    event_publisher = runtime.context.event_publisher
    assert event_publisher is not None, "exec_node requires ctx.event_publisher"  # noqa: S101
    async with node_lifecycle(
        "exec",
        messages=state.messages,
        event_publisher=event_publisher,
        agent_id=agent_id_from_config(config),
        turn_progress=runtime.context.turn_progress,
        stall=StallConfig(runtime.context.require_agent().read("agent", "node_stall_dump_seconds")),
        timeline_inputs=runtime.context.timestamp_policy(),
        timeline_limit=runtime.context.require_agent().read("display", "timeline_default_limit"),
    ):
        return await exec_node_impl(state, runtime, config, process=process)


# meiva:note
# Graph-level exec node timeout — defense-in-depth above the per-code-block
# exec_timeout_seconds. If the inner deadline misses a cancellable framework
# hang, this outer shield requests cancellation and surfaces a timeout after
# the exec's process group is killed and reaped. It is not an independent hard
# bound on an OS kill/reap call that itself wedges. (The interrupt
# subscription sits outside this wait_for and is bounded by its own watcher
# exit timeout.)
async def _exec_with_node_shield(
    coro: Awaitable[tuple[ExecResult, ResultPayload | None]],
    agent_id: int,
    *,
    timeout_seconds: float,
) -> tuple[ExecResult, ResultPayload | None]:
    try:
        return await asyncio.wait_for(coro, timeout=timeout_seconds)
    except TimeoutError:
        logger.error(
            "[exec(node-timeout)] exec_node timed out after {timeout}s — "
            "inner code-exec timeout did not trigger; possible framework hang. "
            "Returning timeout ToolMessage so the LLM can react.",
            event="exec_node_timeout",
            timeout=timeout_seconds,
            agent_id=agent_id,
        )
        return (
            ExecTimedOut(
                output=(
                    f"[exec node timeout after {timeout_seconds:.0f}s] "
                    "Execution was stopped by an internal safeguard and did not "
                    "complete. This does not necessarily mean your code was slow; "
                    "consider re-running it, or moving long-running work to a "
                    "persistent shell session."
                )
            ),
            None,
        )


async def run_agent_code(
    state: _state.AgentState,
    ctx: MeivaContext,
    agent_id: int,
    code: str,
    chunk_publisher: ExecOutputChunkPublisher,
    *,
    process: ExecProcess,
) -> tuple[
    ExecResult,
    dict[str, Any],
    int,
    list[dict[str, Any]] | None,
    list[dict[str, Any]] | None,
]:
    """Run the agent's code in one disposable child process.

    The parent does not touch the meiva.state slot — the child rebuilds the
    snapshot from the request envelope (`agent/execution/child.py`), and the
    state-update delta (plugin fields, security findings) rides the result
    envelope back. Validation is fail-fast on a tampered slot, and the child
    receives the bound turn's config maps so its SDK calls
    resolve the same settings. Returns
    (result, plugin_state_update, exec_ms, attachments, sdk_calls)."""
    config_overlay = ctx.require_agent().overlay()
    exec_started = time.monotonic()
    async with subscribe_interrupt(
        ctx.ops_pool,
        agent_id,
        resources=ctx.hosted_resources,
    ) as cancel_event:
        outcome = await _exec_with_node_shield(
            process.run(
                code,
                ctx,
                cancel_event,
                chunk_publisher,
                state=state.model_dump(),
                config_overlay=config_overlay,
            ),
            agent_id,
            timeout_seconds=ctx.require_agent().read("sandbox", "exec_node_timeout_seconds"),
        )
    # Wall-clock surfaced on the code_output item ("ran in 1.3s"); cancel /
    # timeout still report the honest time-before-stop.
    exec_ms = round((time.monotonic() - exec_started) * 1000)
    result, payload = outcome
    if isinstance(result, ExecCancelled) and cancel_event.is_set():
        result = replace(result, reason=cancel_event.reason)
    if payload is not None and payload.state_update_error is not None:
        # The child reported a tampered slot (agent set meiva.state_update to a
        # non-dict) — raise the TypeError the child would have raised.
        raise TypeError(payload.state_update_error)
    delta = payload.state_update if payload is not None else None
    plugin_state_update = validate_plugin_state_keys(dict(delta), state.__class__) if delta else {}
    attachments = plugin_state_update.pop("attach", None)
    sdk_calls = payload.sdk_calls if payload is not None else None
    return result, plugin_state_update, exec_ms, attachments, sdk_calls


def _emit_exec_boot_failed(agent_id: int, crash: CrashFacts) -> None:
    """The `exec_child_boot_failed` signal: the child died before the agent's code ran."""
    logger.warning(
        "[exec-boot-failed] exec child crashed before running code: {exc_type}: {exc_msg}",
        event="exec_child_boot_failed",
        agent_id=agent_id,
        exc_type=crash.exc_type,
        exc_msg=crash.exc_msg[:200],
    )


def _log_crash(
    plan: DispatchPlan, crash: CrashFacts, envelope: ExecEnvelope, agent_id: int
) -> None:
    logger.info(
        "[{label}] {body}\n[full traceback]\n{full_traceback}",
        label="exec-failed",
        body=envelope.text,
        full_traceback=crash.child_traceback or format_full_traceback(crash.exc),
        event="exec_failed",
        exc_type=type(crash.exc).__name__,
        agent_id=agent_id,
    )
    if plan.boot_failed:
        _emit_exec_boot_failed(agent_id, crash)


# meiva:note
# Word what the plan decided. A failed execute_code logs at INFO with event=exec_failed:
# trial-and-error is the normal dev loop, not an operator alert (metrics still aggregate by
# event name). The log gets the full unfiltered chain, so framework or SDK bugs invisible in
# the agent view stay diagnosable: the child's formatted traceback, or one formatted here for
# a parent-side failure.
def _log_dispatch(plan: DispatchPlan, envelope: ExecEnvelope, agent_id: int) -> None:
    if plan.crash is not None:
        _log_crash(plan, plan.crash, envelope, agent_id)
        return
    match plan.kind:
        case DispatchKind.CANCELLED:
            logger.info(
                "[{label}] {body}",
                label="exec-cancelled",
                body=envelope.text,
                event="exec_cancelled",
                agent_id=agent_id,
            )
        case DispatchKind.TIMED_OUT:
            logger.info(
                "[{label}] {body}",
                label="exec-timeout",
                body=envelope.text,
                event="exec_timeout",
                agent_id=agent_id,
            )
        case (
            DispatchKind.DONE | DispatchKind.COMPACT | DispatchKind.LIFECYCLE | DispatchKind.CRASHED
        ):
            logger.info("[exec] {body}", event="exec", body=envelope.text, agent_id=agent_id)
    if plan.halt_log is not None:
        logger.info("[halt] {body}", event="halt", body=plan.halt_log, agent_id=agent_id)


# meiva:note
# Execute the dispatch plan `plan_dispatch` makes for one `ExecResult`: wrap the envelope the
# plan's markers describe, log the outcome, and tell the frontend about a cancel (symmetric
# with the llm_node cancel path; a timeout is not a user cancel and sends nothing).
def dispatch_exec_result(
    result: ExecResult,
    event_publisher: AgentEventPublisher,
    agent_id: int,
    *,
    elapsed_seconds: float,
    timestamp: str | None,
    referenced_messages: Sequence[AnyMessage],
    limits: OutputLimits,
    now: datetime,
) -> tuple[DispatchPlan, ExecEnvelope]:
    plan = plan_dispatch(result)
    envelope = wrap_code_output(
        plan.body,
        limits=limits,
        now=now,
        elapsed_seconds=elapsed_seconds,
        timestamp=timestamp,
        cancelled=plan.cancelled,
        cancel_reason=plan.cancel_reason,
        timed_out=plan.timed_out,
        stream_cap=result.stream_cap,
        referenced_messages=referenced_messages,
    )
    _log_dispatch(plan, envelope, agent_id)
    if plan.kind is DispatchKind.CANCELLED:
        event_publisher.emit(Cancelled(agent_id=agent_id).model_dump_json())
    return plan, envelope


def _output_limits(agent: AgentSlices, agent_id: int, *, home: Path) -> OutputLimits:
    return OutputLimits(
        archive_dir=overflow_dir(agent_id, home=home),
        crop=CropLimits(
            **{field.name: agent.read("sandbox", field.name) for field in fields(CropLimits)}
        ),
        timeout_seconds=agent.read("sandbox", "exec_timeout_seconds"),
        max_chars=agent.read("sandbox", "exec_output_max_chars"),
    )


def _attach_model(ctx: MeivaContext) -> str:
    """The model name attachments are packed for (media capability gate).

    Same resolution as the claim fallback drain (`_attach_drain.py`): the live
    LLM's model name, else the configured turn model. ``ctx.llm`` can be None
    (tests / container edge), hence the getattr fallback.
    """
    return getattr(ctx.llm, "model_name", None) or ctx.require_agent().brain.llm_model


async def _exec_single_call(
    state: _state.AgentState,
    ctx: MeivaContext,
    agent_id: int,
    call: ToolCall,
    exec_msg_idx: int,
    *,
    process: ExecProcess,
) -> tuple[dict[str, Any], bool]:
    """Execute one invocation; return its state delta and whether it compacted."""
    assert ctx.event_publisher is not None, (  # noqa: S101
        "exec_node_impl requires ctx.event_publisher"
    )
    # Deferring notes/media keeps result positions consecutive. Streaming and
    # checkpoint reconstruction must use those same positions.
    ctx.event_publisher.emit(
        ExecStart(agent_id=agent_id, item_id=f"{exec_msg_idx}.0").model_dump_json()
    )

    state_messages_update: list[AnyMessage] = []
    clock = ctx.require_clock()
    if call["name"] != "execute_code" or "code" not in call["args"]:
        error = exec_output_message(
            content=f"unknown tool {call['name']!r}; only `execute_code(code: str)` is registered",
            tool_call_id=call["id"] or "",
            status=ExecStatus.NOT_RUN,
            body_start=0,
            created_at=clock.now(),
        )
        ctx.event_publisher.emit(
            ExecOutput(
                agent_id=agent_id, item_id=f"{exec_msg_idx}.0", content=str(error.content)
            ).model_dump_json()
        )
        return {"messages": [error], "halted": False}, False

    # Streaming chunks and the final ExecOutput share the same item_id
    # computed above; the frontend uses it to append chunks to the same
    # code_output item; on completion, ExecOutput upserts at the same id,
    # replacing with wrap_code_output envelope version.
    chunk_publisher = ExecOutputChunkPublisher(
        ctx.event_publisher,
        agent_id,
        item_id=f"{exec_msg_idx}.0",
    )

    started_at = clock.now()
    (
        result,
        plugin_state_update,
        exec_ms,
        envelope_attachments,
        envelope_sdk_calls,
    ) = await run_agent_code(
        state,
        ctx,
        agent_id,
        code_from_args(call["args"], source=f"tool_call {call['id']!r}"),
        chunk_publisher,
        process=process,
    )
    agent = ctx.require_agent()
    plan, envelope = dispatch_exec_result(
        result,
        ctx.event_publisher,
        agent_id,
        elapsed_seconds=exec_ms / 1000,
        timestamp=clock.now_timestamp() if agent.read("general", "message_timestamps") else None,
        referenced_messages=state.messages,
        limits=_output_limits(agent, agent_id, home=ctx.require_meiva_home()),
        now=clock.now().astimezone(clock.explicit_zone()),
    )
    halted, status = plan.halted, plan.status

    # Pop the plugin's messages delta out of the state update — merged below
    # after the ToolMessage instead of riding the dict **spread (which would
    # clobber the ToolMessage). Popped + drained unconditionally so a compact
    # turn (REMOVE_ALL'd by claim) leaks nothing to later turns.
    plugin_messages = plugin_state_update.pop("messages", None)

    # Compact path (SystemHalt): write nothing back — claim REMOVE_ALLs the
    # whole history this turn, so ToolMessage/notes would be wiped anyway.
    compact_halt = not plan.writes_back
    if compact_halt:
        # Findings annotate the history claim is about to wipe; the rest of the delta (plugin
        # fields) is written back as before, and the findings otherwise ride the spread below
        # into `state.security_findings` for the after_exec hook.
        plugin_state_update.pop("security_findings", None)
    else:
        # The UI shows exactly what the agent sees in exec output — same blob
        # fed back to the LLM below (ExecOutput shares item_id with the chunk).
        ctx.event_publisher.emit(
            ExecOutput(
                agent_id=agent_id,
                item_id=f"{exec_msg_idx}.0",
                content=envelope.text,
            ).model_dump_json()
        )

        msg = exec_output_message(
            content=envelope.text,
            tool_call_id=call["id"] or "",
            status=status,
            body_start=envelope.body_start,
            started_at=started_at,
            exec_ms=exec_ms,
            sdk_calls=envelope_sdk_calls,
            created_at=clock.now(),
        )
        state_messages_update.append(msg)

        # In-memory system-note injection (user ruling 2026-08-11): plugin
        # context notes merge into this exec's delta after the ToolMessage
        # (ordering rationale: _notes.py).
        state_messages_update = merge_exec_notes(state_messages_update, plugin_messages)
    update: dict[str, Any] = {
        "messages": state_messages_update,
        "halted": halted,
        **plugin_state_update,
    }
    if plan.cancelled:
        update.update(interrupt_halt(plan.cancel_reason))
    if not compact_halt:
        # Attachments registered during this execute_code call are packed into
        # a media HumanMessage appended right after the exec output — the model
        # sees the attached files on its very next step of the SAME turn (user
        # ruling 2026-08-26). The claim-node drain (_attach_drain.py) remains
        # as the fallback for edge paths that skip this update (compact halt).
        merged_attach = merge_attachments(state.attach, envelope_attachments)
        update["attach"] = merged_attach
        attach_msg = build_attach_message(
            merged_attach,
            _attach_model(ctx),
            catalog=ctx.require_catalog(),
            created_at=ctx.require_clock().now(),
        )
        if attach_msg is not None:
            state_messages_update.append(attach_msg)
            update["attach"] = AttachState()
    return update, compact_halt


def _remaining_exec_calls(
    messages: Sequence[AnyMessage],
) -> tuple[list[AnyMessage], list[ToolCall]]:
    """Derive progress from committed result IDs, without a separate cursor."""
    completed: set[str] = set()
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            completed.add(message.tool_call_id)
        elif isinstance(message, AIMessage):
            normalized = normalize_tool_calls(message)
            calls = (normalized or message).tool_calls
            if not calls:
                raise ValueError("exec_node: previous AIMessage has no tool_calls")
            return (
                [normalized] if normalized is not None else [],
                [call for call in calls if (call["id"] or "") not in completed],
            )
    raise ValueError("exec_node: no preceding AIMessage")


def _skipped_call_results(
    calls: Sequence[ToolCall], ctx: MeivaContext, agent_id: int, start_index: int
) -> list[ToolMessage]:
    """Pair calls left unexecuted by a lifecycle halt or cancellation."""
    assert ctx.event_publisher is not None  # noqa: S101
    now = ctx.require_clock().now()
    results: list[ToolMessage] = []
    for ordinal, call in enumerate(calls):
        message = exec_output_message(
            content="Not executed: an earlier tool call halted or cancelled this turn.",
            tool_call_id=call["id"] or "",
            status=ExecStatus.NOT_RUN,
            body_start=0,
            created_at=now,
        )
        results.append(message)
        ctx.event_publisher.emit(
            ExecOutput(
                agent_id=agent_id,
                item_id=f"{start_index + ordinal}.0",
                content=str(message.content),
            ).model_dump_json()
        )
    return results


# meiva:note
# Run one call; LangGraph owns reducer application and the next snapshot. Recovery may already
# have paired an interrupted remainder when `calls` is empty, and compaction wipes any findings
# raised by earlier calls of this batch along with the history.
async def exec_node_impl(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
    *,
    process: ExecProcess,
) -> Command[ExecGoto]:
    replacements, calls = _remaining_exec_calls(state.messages)
    if not calls:
        return Command[ExecGoto](
            update={"messages": state.pending_exec_notes, "pending_exec_notes": []},
            goto=AFTER_EXEC,
        )
    agent_id = agent_id_from_config(config)
    delta, compacted = await _exec_single_call(
        state, runtime.context, agent_id, calls[0], len(state.messages), process=process
    )
    if compacted:
        delta["pending_exec_notes"] = []
        delta["security_findings"] = Overwrite([])
        return Command[ExecGoto](update=delta, goto=AFTER_EXEC)

    results = [message for message in delta["messages"] if isinstance(message, ToolMessage)]
    notes = merge_exec_notes(
        state.pending_exec_notes,
        [message for message in delta["messages"] if not isinstance(message, ToolMessage)],
    )
    if delta["halted"]:
        results.extend(
            _skipped_call_results(
                calls[1:], runtime.context, agent_id, len(state.messages) + len(results)
            )
        )
    elif len(calls) > 1:
        delta["messages"] = replacements + results
        delta["pending_exec_notes"] = notes
        return Command[ExecGoto](update=delta, goto=EXEC)

    delta["messages"] = replacements + results + notes
    delta["pending_exec_notes"] = []
    return Command[ExecGoto](update=delta, goto=AFTER_EXEC)


_exec_node_impl = exec_node_impl
