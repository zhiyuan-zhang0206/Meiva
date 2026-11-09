# meiva:note
# `agent.hooks.make_hook_runner` knows how to run a hook list at one graph edge and nothing about the graph. A container node is part of the graph, so its death observability matches claim, llm and exec: `node_lifecycle` wraps each pass with enter/exit events and publishes a timeline snapshot on enter. `hook_name` matches the NodeName (before_turn) and is used directly as the node name to emit.
# meiva:link agent/hooks/docs/hooks.md

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence

from langchain_core.messages import BaseMessage, RemoveMessage
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent import state as _state
from agent.graph.node_log import StallConfig, node_lifecycle
from agent.hooks import HookName, make_hook_runner
from agent.nodes import NodeName
from base.agents.context import MeivaContext, agent_id_from_config
from base.api_contracts.agent_contract import Harness
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.packages.plugins.extensions import GraphHook


def _render_message(msg: BaseMessage) -> str:
    content: object = msg.content
    if isinstance(content, str):
        return content.strip()
    if not is_object_list(content):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif is_object_dict(block):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts).strip()


def _render_messages(messages: Sequence[BaseMessage]) -> str:
    return "\n\n".join(part for msg in messages if (part := _render_message(msg)))


def _hook_input_state(state: _state.AgentState, *, is_external: bool) -> _state.AgentState:
    if not is_external or state.messages:
        return state
    pending_input: object = state.harness_pending_input
    if not is_object_dict(pending_input):
        return state
    raw_msgs = pending_input.get("messages")
    if not is_object_list(raw_msgs) or not raw_msgs:
        return state
    return state.model_copy(
        update={"messages": [m for m in raw_msgs if isinstance(m, BaseMessage)]}
    )


def _fold_external_hook_messages(
    state: _state.AgentState,
    cmd_update: dict[str, object],
) -> None:
    emitted = cmd_update.pop("messages", None)
    pre_messages = (
        [m for m in emitted if isinstance(m, BaseMessage) and not isinstance(m, RemoveMessage)]
        if is_object_list(emitted)
        else []
    )
    pre_text = _render_messages(pre_messages)
    pending_input: object = state.harness_pending_input
    if is_object_dict(pending_input):
        if pre_text or "messages" in pending_input:
            updated_pending: dict[str, object] = {
                k: v for k, v in pending_input.items() if k != "messages"
            }
            if pre_text:
                prompt = str(updated_pending.get("prompt") or "")
                updated_pending["prompt"] = f"{pre_text}\n\n{prompt}" if prompt else pre_text
            cmd_update["harness_pending_input"] = updated_pending
    elif pre_text:
        cmd_update["harness_pending_input"] = {"prompt": pre_text}


def hook_node(
    hook_name: HookName,
    default_next: NodeName | Callable[[_state.AgentState], NodeName],
    hooks: Sequence[tuple[str | None, GraphHook]],
) -> Callable[
    [_state.AgentState, Runtime[MeivaContext], RunnableConfig], Awaitable[Command[NodeName]]
]:
    """A LangGraph node that runs `hooks` at one edge, wrapped in the node lifecycle events."""
    runner = make_hook_runner(hook_name, default_next, hooks)

    async def run(
        state: _state.AgentState,
        runtime: Runtime[MeivaContext],
        config: RunnableConfig,
    ) -> Command[NodeName]:
        event_publisher = runtime.context.event_publisher
        assert event_publisher is not None, (  # noqa: S101
            f"hook runner ({hook_name}) requires ctx.event_publisher"
        )
        is_external = runtime.context.harness is not Harness.MEIVA
        hook_state = _hook_input_state(state, is_external=is_external)
        async with node_lifecycle(
            hook_name,
            messages=hook_state.messages,
            event_publisher=event_publisher,
            agent_id=agent_id_from_config(config),
            turn_progress=runtime.context.turn_progress,
            stall=StallConfig(
                runtime.context.require_agent().read("agent", "node_stall_dump_seconds")
            ),
            timeline_inputs=runtime.context.timestamp_policy(),
            timeline_limit=runtime.context.require_agent().read(
                "display", "timeline_default_limit"
            ),
            publish_timeline=not is_external,
        ):
            cmd = await runner(hook_state, runtime, config)
            cmd_update: object = cmd.update
            if is_external and is_object_dict(cmd_update):
                _fold_external_hook_messages(state, cmd_update)
            return cmd

    return run
