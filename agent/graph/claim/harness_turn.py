from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from datetime import datetime
from typing import Any, Literal, TypeGuard

from langchain_core.messages import AIMessage, BaseMessage, ToolCall, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph.claim.turn_stamp import persist_last_active
from agent.graph.hooks.framework import harness_pre_turn_hooks
from agent.graph.interrupt import (
    ModelInterruptedError,
    interrupt_halt,
    interruptible_model,
    subscribe_interrupt,
)
from agent.graph.llm_errors import FatalLLMStreamError
from agent.graph.node_log import StallConfig, node_lifecycle
from agent.messages import exec_output_message
from agent.nodes import CLAIM, HARNESS_TURN
from base.agents.context import MeivaContext, agent_id_from_config
from base.agents.history.timeline_item import TimelineItem
from base.agents.messages.inbound import InterruptReason
from base.agents.messages.kwargs import ExecStatus, read_meiva_kwargs
from base.agents.observation.turn_progress import TurnProgress
from base.agents.sdk.telemetry import SdkCall
from base.clock import Clock
from base.events.live.coalescer import DeltaCoalescer
from base.events.live.projection import (
    Cancelled,
    ChatDelta,
    ChatStart,
    CodeDelta,
    CodeStart,
    ExecOutput,
    ExecStart,
    LLMDone,
    ReasoningDelta,
    ReasoningStart,
    TimelineSnapshot,
)
from base.events.live.publisher import AgentEventPublisher
from base.lm.catalog import ModelCatalog
from base.lm.content import message_content
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    HarnessClosedError,
    HarnessSetupError,
    HarnessTurnFaultedError,
    ReasoningText,
    TextDelta,
    ToolResult,
    ToolUse,
)
from base.lm.harness import (
    ReasoningDelta as BridgeReasoningDelta,
)
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
    return [
        message
        for message in state.messages[cursor:]
        if not isinstance(message, (AIMessage, ToolMessage))
    ]


def render_messages(messages: Sequence[BaseMessage]) -> str:
    return "\n\n".join(render_message(message) for message in messages).strip()


def render_message(message: BaseMessage) -> str:
    content = message_content(message)
    if isinstance(content, str):
        return content
    return "\n".join(
        block
        if isinstance(block, str)
        else (
            str(block["text"])
            if block.get("type") == "text" and isinstance(block.get("text"), str)
            else f"[{block.get('type', 'attachment')}]"
        )
        for block in content
    )


# meiva:note
# The adapter's idempotency key: the newest inbound row in the batch, which is unique
# per claim and stable when the same window is submitted twice (a crashed turn, a
# retry within the session). A window that carries only notes has no row to name, so
# the cursor plus its length stands in; retrying the same window at the same cursor
# reuses the key, while a later note-only turn (such as a second heartbeat) gets a
# distinct key instead of replaying the first.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
def turn_key(messages: Sequence[BaseMessage], *, cursor: int = 0) -> str:
    for message in reversed(messages):
        inbound_id = read_meiva_kwargs(message).get("meiva_inbound_id")
        if isinstance(inbound_id, int):
            return f"inbound-{inbound_id}"
    return f"window-{cursor + len(messages)}"


def _first_str(mapping: Mapping[str, Any], *keys: str) -> str | None:
    return next((val for k in keys if isinstance(val := mapping.get(k), str) and val), None)


def _format_write_tool_use(tool_input: Mapping[str, Any]) -> str:
    path = _first_str(tool_input, "file_path", "Path", "TargetFile")
    content = _first_str(tool_input, "content", "Content", "CodeContent")
    return f"# {path}\n{content.rstrip()}" if path and content is not None else ""


def _format_edit_tool_use(tool_input: Mapping[str, Any]) -> str:
    path = _first_str(tool_input, "file_path", "Path", "TargetFile")
    old = _first_str(tool_input, "old_string", "Target", "TargetContent")
    new = _first_str(tool_input, "new_string", "Replacement", "ReplacementContent")
    if path and old is not None and isinstance(new, str):
        return f"# {path}\n--- old\n{old.rstrip()}\n+++ new\n{new.rstrip()}"
    return ""


def format_tool_use(name: str, tool_input: Mapping[str, Any]) -> str:
    if (
        name in {"Bash", "command", "run_command"}
        and (cmd := _first_str(tool_input, "command", "CommandLine"))
        and cmd.strip()
    ):
        return cmd
    if name == "patch" and (rendered := _format_patch_tool_use(tool_input)):
        return rendered
    if name in {"Write", "write_file", "write_to_file"} and (
        r := _format_write_tool_use(tool_input)
    ):
        return r
    if name in {"Edit", "edit_file", "replace_file_content"} and (
        r := _format_edit_tool_use(tool_input)
    ):
        return r
    return json.dumps(dict(tool_input), ensure_ascii=False, indent=2) if tool_input else name


def _is_object_mapping(value: object) -> TypeGuard[Mapping[object, object]]:
    return isinstance(value, Mapping)


def _is_object_sequence(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def _format_patch_change(raw_entry: object) -> str | None:
    if not _is_object_mapping(raw_entry):
        return None
    entry: dict[str, Any] = {str(k): v for k, v in raw_entry.items()}
    if not (path := _first_str(entry, "path")):
        return None
    header = f"# {kind}: {path}" if (kind := _first_str(entry, "kind")) else f"# {path}"
    diff = entry.get("diff")
    return f"{header}\n{diff.rstrip()}" if isinstance(diff, str) and diff.strip() else header


def _format_patch_tool_use(tool_input: Mapping[str, Any]) -> str:
    changes = tool_input.get("changes")
    if _is_object_sequence(changes) and changes:
        blocks = [r for raw in changes if (r := _format_patch_change(raw)) is not None]
        if blocks:
            return "\n\n".join(blocks)
    raw_paths = tool_input.get("paths")
    if _is_object_sequence(raw_paths) and raw_paths and all(isinstance(x, str) for x in raw_paths):
        return "\n".join(x for x in raw_paths if isinstance(x, str))
    return ""


# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
class HarnessTurnProjector:
    def __init__(
        self,
        *,
        event_publisher: AgentEventPublisher,
        agent_id: int,
        base_idx: int,
        turn_progress: TurnProgress,
        clock: Clock,
    ) -> None:
        self._publisher = event_publisher
        self._agent_id = agent_id
        self._msg_idx = base_idx
        self._turn_progress = turn_progress
        self._clock = clock
        self._step_started_at = self._clock.now()
        self._reasoning_coalescer = DeltaCoalescer(self._emit_reasoning_delta)
        self._coalescer = DeltaCoalescer(self._emit_chat_delta)
        self._reasoning_started = False
        self._streamed_reasoning = False
        self._reasoning_started_at: datetime | None = None
        self._reasoning_ended_at: datetime | None = None
        self._reasoning_buf: list[str] = []
        self._chat_started = False
        self._streamed_text = False
        self._text_completed = False
        self._any_text_event = False
        self._text_buf: list[str] = []
        self._messages: list[BaseMessage] = []
        self._tool_slots: dict[str, tuple[int, str, datetime]] = {}

    def _emit_reasoning_delta(self, item_id: str, content: str) -> None:
        self._publisher.emit(
            ReasoningDelta(
                agent_id=self._agent_id, item_id=item_id, content=content
            ).model_dump_json()
        )

    def _emit_chat_delta(self, item_id: str, content: str) -> None:
        self._publisher.emit(
            ChatDelta(agent_id=self._agent_id, item_id=item_id, content=content).model_dump_json()
        )

    def on_event(self, event: BridgeEvent) -> None:
        self._turn_progress.mark(self._agent_id)
        if isinstance(event, (BridgeReasoningDelta, ReasoningText)):
            self._on_reasoning_event(event)
        elif isinstance(event, (TextDelta, AssistantText)):
            self._on_text_event(event)
        elif isinstance(event, ToolUse):
            self._on_tool_use(event)
        elif isinstance(event, ToolResult):
            self._on_tool_result(event)

    def _on_reasoning_event(self, event: BridgeReasoningDelta | ReasoningText) -> None:
        if self._text_completed and self._text_buf:
            self._flush_completed_text()
        if isinstance(event, BridgeReasoningDelta):
            self._streamed_reasoning = True
            self._append_reasoning(event.text)
            return
        if not self._streamed_reasoning:
            self._append_reasoning(event.text)
        self._streamed_reasoning = False
        self._reasoning_ended_at = self._clock.now()

    def _on_text_event(self, event: TextDelta | AssistantText) -> None:
        if self._text_completed and self._text_buf:
            self._flush_completed_text()
        if self._reasoning_started and self._reasoning_ended_at is None:
            self._reasoning_ended_at = self._clock.now()
        self._any_text_event = True
        if isinstance(event, TextDelta):
            self._streamed_text = True
            self._append_text(event.text)
            return
        if not self._streamed_text:
            self._append_text(event.text)
        self._streamed_text = False
        self._text_completed = True

    def _reset_step_buffers(self, now: datetime) -> None:
        self._step_started_at = now
        self._reasoning_buf.clear()
        self._reasoning_started = False
        self._streamed_reasoning = False
        self._reasoning_started_at = None
        self._reasoning_ended_at = None
        self._text_buf.clear()
        self._chat_started = False
        self._streamed_text = False
        self._text_completed = False

    def _build_ai_content_and_kwargs(
        self,
        step_text: str,
        now: datetime,
        *,
        include_code_ms_at: int | None = None,
    ) -> tuple[str | list[str | dict[str, Any]], dict[str, Any], int | None, int | None]:
        self._reasoning_coalescer.flush()
        self._coalescer.flush()
        reasoning_text = "".join(self._reasoning_buf).strip()
        kwargs: dict[str, Any] = {"meiva_created_at": now.isoformat()}
        reasoning_ms: int | None = None
        if reasoning_text:
            r_start = self._reasoning_started_at or self._step_started_at
            r_end = self._reasoning_ended_at or now
            reasoning_ms = max(int((r_end - r_start).total_seconds() * 1000), 0)
            kwargs["meiva_reasoning_ms_by_block"] = {"0": reasoning_ms}
            blocks: list[str | dict[str, Any]] = [
                {"type": "thinking", "thinking": reasoning_text, "index": 0}
            ]
            if step_text:
                blocks.append({"type": "text", "text": step_text, "index": 1})
            content: str | list[str | dict[str, Any]] = blocks
        else:
            content = step_text
        code_ms: int | None = None
        if include_code_ms_at is not None:
            c_start = self._reasoning_ended_at or self._step_started_at
            if (computed := max(int((now - c_start).total_seconds() * 1000), 0)) > 0:
                code_ms = computed
                kwargs["meiva_code_ms_by_block"] = {str(include_code_ms_at): code_ms}
        return content, kwargs, reasoning_ms, code_ms

    def _flush_completed_text(self) -> None:
        now = self._clock.now()
        step_text = "".join(self._text_buf).strip()
        if step_text or "".join(self._reasoning_buf).strip():
            content, kwargs, _, _ = self._build_ai_content_and_kwargs(step_text, now)
            self._messages.append(AIMessage(content=content, additional_kwargs=kwargs))
            self._msg_idx += 1
        self._reset_step_buffers(now)

    def _append_reasoning(self, text: str) -> None:
        if not text:
            return
        item_id = f"{self._msg_idx}.0"
        if not self._reasoning_started:
            self._reasoning_started_at = self._step_started_at
            self._publisher.emit(
                ReasoningStart(agent_id=self._agent_id, item_id=item_id).model_dump_json()
            )
            self._reasoning_started = True
        self._reasoning_buf.append(text)
        self._reasoning_coalescer.append(item_id, text)

    def _append_text(self, text: str) -> None:
        if not text:
            return
        item_id = f"{self._msg_idx}.{1 if self._reasoning_started else 0}"
        if not self._chat_started:
            self._reasoning_coalescer.flush()
            self._publisher.emit(
                ChatStart(agent_id=self._agent_id, item_id=item_id).model_dump_json()
            )
            self._chat_started = True
        self._text_buf.append(text)
        self._coalescer.append(item_id, text)

    def _on_tool_use(self, event: ToolUse) -> None:
        now = self._clock.now()
        reasoning_text = "".join(self._reasoning_buf).strip()
        step_text = "".join(self._text_buf).strip()
        code_block_idx = (1 if reasoning_text else 0) + (1 if step_text else 0)
        content, kwargs, reasoning_ms, code_ms = self._build_ai_content_and_kwargs(
            step_text, now, include_code_ms_at=code_block_idx
        )
        code_item_id = f"{self._msg_idx}.{code_block_idx}"
        exec_item_id = f"{self._msg_idx + 1}.0"
        code_text = format_tool_use(event.name, event.tool_input)
        tool_id = event.tool_use_id or f"call-{self._msg_idx}"
        tool_source = event.name if event.name != "execute_code" else None
        self._publisher.emit(
            CodeStart(agent_id=self._agent_id, item_id=code_item_id).model_dump_json()
        )
        self._publisher.emit(
            CodeDelta(
                agent_id=self._agent_id, item_id=code_item_id, content=code_text
            ).model_dump_json()
        )
        snapshot_items: list[dict[str, Any]] = []
        if reasoning_text:
            snapshot_items.append(
                TimelineItem(
                    item_id=f"{self._msg_idx}.0",
                    kind="agent_reasoning",
                    payload=reasoning_text,
                    created_at=now.isoformat(),
                    reasoning_ms=reasoning_ms,
                ).model_dump()
            )
        if step_text:
            snapshot_items.append(
                TimelineItem(
                    item_id=f"{self._msg_idx}.{1 if reasoning_text else 0}",
                    kind="agent_chat",
                    payload=step_text,
                    created_at=now.isoformat(),
                ).model_dump()
            )
        snapshot_items.append(
            TimelineItem(
                item_id=code_item_id,
                kind="agent_code",
                source=tool_source,
                payload=code_text,
                created_at=now.isoformat(),
                code_elapsed_ms=code_ms,
                sdk_calls=[SdkCall(method=event.name, count=1)],
            ).model_dump()
        )
        self._publisher.emit(
            TimelineSnapshot(
                agent_id=self._agent_id, items=snapshot_items, msg_count=self._msg_idx + 1
            ).model_dump_json()
        )
        self._publisher.emit(
            ExecStart(agent_id=self._agent_id, item_id=exec_item_id).model_dump_json()
        )
        call = ToolCall(name=event.name, args={"code": code_text}, id=tool_id, type="tool_call")
        self._messages.append(
            AIMessage(content=content, tool_calls=[call], additional_kwargs=kwargs)
        )
        slot_idx = len(self._messages)
        self._messages.append(
            exec_output_message(
                content="",
                tool_call_id=tool_id,
                status=ExecStatus.COMPLETED,
                body_start=0,
                created_at=now,
                started_at=now,
                exec_ms=0,
                sdk_calls=[{"method": event.name, "count": 1}],
            )
        )
        self._tool_slots[event.tool_use_id] = (slot_idx, exec_item_id, now)
        self._msg_idx += 2
        self._reset_step_buffers(now)

    def _on_tool_result(self, event: ToolResult) -> None:
        if (slot := self._tool_slots.get(event.tool_use_id)) is None:
            return
        slot_idx, exec_item_id, started_at = slot
        now = self._clock.now()
        self._step_started_at = now
        exec_ms = max(int((now - started_at).total_seconds() * 1000), 0)
        prev = self._messages[slot_idx]
        tool_id = prev.tool_call_id if isinstance(prev, ToolMessage) else event.tool_use_id
        self._publisher.emit(
            ExecOutput(
                agent_id=self._agent_id, item_id=exec_item_id, content=event.content
            ).model_dump_json()
        )
        self._messages[slot_idx] = exec_output_message(
            content=event.content,
            tool_call_id=tool_id,
            status=ExecStatus.FAILED if event.is_error else ExecStatus.COMPLETED,
            body_start=0,
            created_at=now,
            started_at=started_at,
            exec_ms=exec_ms,
            sdk_calls=read_meiva_kwargs(prev).get("sdk_calls"),
        )

    def settle_interrupt(
        self,
        reason: InterruptReason,
        *,
        effective_len: int,
        pre_messages: Sequence[BaseMessage],
    ) -> dict[str, object]:
        halt: dict[str, object] = dict(interrupt_halt(reason))
        partial: list[BaseMessage] = []
        if reason is InterruptReason.SYSTEM and self._messages:
            self._reasoning_coalescer.flush()
            self._coalescer.flush()
            self._publisher.emit(LLMDone(agent_id=self._agent_id).model_dump_json())
            partial = list(self._messages)
        if pre_messages or partial:
            raw_halt = halt.get("messages")
            halt_msgs = (
                [m for m in raw_halt if isinstance(m, BaseMessage)]
                if _is_object_sequence(raw_halt)
                else []
            )
            halt["messages"] = [*pre_messages, *partial, *halt_msgs]
        if partial:
            halt["harness_cursor"] = effective_len + len(partial)
        return halt

    def finish(self, turns: Sequence[HarnessTurn]) -> list[BaseMessage]:
        self._reasoning_coalescer.flush()
        self._coalescer.flush()
        self._publisher.emit(LLMDone(agent_id=self._agent_id).model_dump_json())
        reasoning_text = "".join(self._reasoning_buf).strip()
        if not self._messages and not reasoning_text:
            return [AIMessage(content=completed.text) for completed in turns]
        final_text = "".join(self._text_buf).strip()
        if not final_text and not self._any_text_event:
            final_text = turns[-1].text
        if final_text or reasoning_text:
            now = self._clock.now()
            content, kwargs, _, _ = self._build_ai_content_and_kwargs(final_text, now)
            self._messages.append(AIMessage(content=content, additional_kwargs=kwargs))
        return list(self._messages)


async def _run_pre_turn_hooks(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> tuple[list[BaseMessage], dict[str, Any]]:
    ctx = runtime.context
    if ctx.ops_pool is None:
        return [], {}
    pre_messages: list[BaseMessage] = []
    hook_updates: dict[str, Any] = {}
    for _plugin, hook in harness_pre_turn_hooks(ctx.plugin_registry()):
        result = await hook(state, runtime, config)
        if not result:
            continue
        for key, value in result.items():
            if key == "messages" and _is_object_sequence(value):
                pre_messages.extend(m for m in value if isinstance(m, BaseMessage))
            elif key != "goto":
                hook_updates[key] = value
    return pre_messages, hook_updates


# meiva:note
# One graph step, one wait: entering `node_lifecycle` publishes the incremental
# `TimelineSnapshot` so newly claimed inbound messages echo immediately in the UI,
# while `HarnessTurnProjector` streams intra-turn text deltas and tool calls/results
# into both live SSE events and committed `state.messages`. A session that faults or
# closes under the turn is raised as a fatal stream abort: the turn cannot finish, so
# it settles as a failure rather than leaving half a reply, and the next inbound may
# start a fresh session.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
async def harness_turn_node(
    state: _state.AgentState,
    runtime: Runtime[MeivaContext],
    config: RunnableConfig,
) -> Command[HarnessGoto]:
    ctx = runtime.context
    event_publisher = ctx.event_publisher
    assert event_publisher is not None, "harness_turn requires ctx.event_publisher"  # noqa: S101
    agent_id = agent_id_from_config(config)
    pre_messages, hook_updates = await _run_pre_turn_hooks(state, runtime, config)
    effective_messages = [*state.messages, *pre_messages] if pre_messages else state.messages
    async with node_lifecycle(
        HARNESS_TURN,
        messages=effective_messages,
        event_publisher=event_publisher,
        agent_id=agent_id,
        turn_progress=ctx.turn_progress,
        stall=StallConfig(ctx.require_agent().read("agent", "node_stall_dump_seconds")),
        timeline_inputs=ctx.timestamp_policy(),
        timeline_limit=ctx.require_agent().read("display", "timeline_default_limit"),
    ):
        return await _run_harness_turn(
            state,
            ctx,
            agent_id,
            event_publisher,
            pre_messages=pre_messages,
            hook_updates=hook_updates,
        )


async def _run_harness_turn(
    state: _state.AgentState,
    ctx: MeivaContext,
    agent_id: int,
    event_publisher: AgentEventPublisher,
    *,
    pre_messages: Sequence[BaseMessage] = (),
    hook_updates: Mapping[str, Any] | None = None,
) -> Command[HarnessGoto]:
    bridge = ctx.require_harness_bridge()
    ctx.turn_progress.mark(agent_id)
    effective_messages = [*state.messages, *pre_messages] if pre_messages else state.messages
    cursor = min(max(state.harness_cursor, 0), len(effective_messages))
    pending = [
        message
        for message in effective_messages[cursor:]
        if not isinstance(message, (AIMessage, ToolMessage))
    ]
    text = render_messages(pending)
    extra_updates = dict(hook_updates) if hook_updates else {}
    if not text:
        if pre_messages:
            extra_updates["messages"] = list(pre_messages)
        return Command[HarnessGoto](
            update={**extra_updates, "halted": True, "harness_cursor": len(effective_messages)},
            goto=CLAIM,
        )

    projector = HarnessTurnProjector(
        event_publisher=event_publisher,
        agent_id=agent_id,
        base_idx=len(effective_messages),
        turn_progress=ctx.turn_progress,
        clock=ctx.require_clock(),
    )
    try:
        async with subscribe_interrupt(
            ctx.ops_pool,
            agent_id,
            resources=ctx.hosted_resources,
        ) as cancel_event:
            turn = await interruptible_model(
                _ticking(
                    ctx.turn_progress.mark,
                    agent_id,
                    bridge.run_turn(
                        text,
                        key=turn_key(pending, cursor=cursor),
                        on_event=projector.on_event,
                    ),
                ),
                cancel_event,
            )
    except ModelInterruptedError as interrupted:
        halt = projector.settle_interrupt(
            interrupted.reason,
            effective_len=len(effective_messages),
            pre_messages=pre_messages,
        )
        if "harness_cursor" not in halt:
            _announce_cancel(ctx, agent_id)
        await interrupt_engine(agent_id, bridge)
        return Command[HarnessGoto](update={**extra_updates, **halt}, goto=CLAIM)
    except (HarnessTurnFaultedError, HarnessClosedError, HarnessSetupError) as exc:
        raise FatalLLMStreamError(f"{ctx.harness.value} harness turn aborted: {exc}") from exc

    turns = (*turn.prelude, turn)
    for completed in turns:
        log_turn_usage(ctx, agent_id, bridge.model, completed)
    committed = projector.finish(turns)
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
            **extra_updates,
            "messages": [*pre_messages, *committed],
            "harness_cursor": len(effective_messages) + len(committed),
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
def _resolve_pricing_model(raw_model: str, catalog: ModelCatalog) -> str:
    candidate = raw_model.removesuffix("[1m]")
    for suffix in ("", "-high", "-medium", "-low", "-minimal"):
        if candidate.endswith(suffix) and (s := candidate.removesuffix(suffix)) in catalog.prices:
            return raw_model if not suffix and raw_model in catalog.prices else s
    return raw_model


def log_turn_usage(ctx: MeivaContext, agent_id: int, model: str | None, turn: HarnessTurn) -> None:
    usage = turn.usage
    if turn.replayed or (usage.input_tokens == 0 and usage.output_tokens == 0):
        return
    catalog = ctx.require_catalog()
    pricing_model = _resolve_pricing_model(model or ctx.require_agent().brain.llm_model, catalog)
    entries = (
        [(step, 1) for step in turn.steps if step.input_tokens > 0 or step.output_tokens > 0]
        if turn.steps
        else [(usage, max(turn.num_turns, 1))]
    )
    try:
        for step_usage, calls in entries:
            log_usage_fields(
                pricing_model,
                tok_in=step_usage.input_tokens,
                tok_out=step_usage.output_tokens,
                tok_cached=step_usage.cache_read_input_tokens or None,
                cache_write_5m=step_usage.cache_creation_input_tokens,
                usage_kind="agent",
                source=ctx.harness.value,
                for_agent_id=agent_id,
                calls=calls,
                catalog=catalog,
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
