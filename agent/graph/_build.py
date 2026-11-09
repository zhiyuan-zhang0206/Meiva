# meiva:note
# build_graph: assemble the self-cycling topology, all Command(goto=) routing.
#
# Deps (ops_pool / llm / event_publisher) are injected via `Runtime[MeivaContext]` —
# build_graph does not take them; the caller passes them via
# `graph.ainvoke(..., context=MeivaContext(...))`. Node functions access them via
# `runtime.context.X`.
#
# Plugins reach the graph as a value: the caller loads them (`meiva.sdk_surface.extensions.load_extensions()`),
# builds the `ExtensionRegistry` of what they declare (`base.packages.plugins.loading.registry.build_registry()`)
# and passes it in. The graph is a function of that registry: its hooks run in the hook container
# nodes, its state classes shape the dynamic `AgentState`. The loader lives in `agent.extensions`
# (task #3633 moved it off this module so surface-only processes never need the graph kernel); the
# import mechanics and the fail-soft contract live in `base.packages.plugins.loading.plugin_loader`
# (`load_plugin_module` / `safe_load_plugin_module`): one module name, package context,
# `sys.modules` identity, and containment however many times a plugin loads.
# A repeat load re-executes the module already in `sys.modules` rather than
# binding a new one, so a plugin module's identity is stable for the life of the
# process. Layer A wrap monkey-patches the process's meiva module; the exec child
# re-runs the plugin surface load at its own boot, so agent code there sees the
# wrapped version too. Config decides what is imported; not imported = not
# declared.
# meiva:link agent/graph/docs/graph.md


import random
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command

from agent.graph.hooks.framework import hooks_at
from agent.graph.hooks.node import hook_node
from agent.nodes import (
    BEFORE_TURN,
    CLAIM,
    EXEC,
    HARNESS_TURN,
    INIT_CONTEXT,
    LLM,
)
from agent.state import BaseAgentState, build_agent_state
from base.agents.context import MeivaContext
from base.packages.plugins.extensions import ExtensionRegistry

from ._init_context import init_context_node
from .claim.harness_turn import harness_turn_node
from .claim.node import claim_node
from .exec.node import ExecGoto, exec_node
from .exec.process import ParentFacts, SubprocessExecProcess
from .llm.goto import LlmGoto
from .llm.node import llm_node


def build_graph(
    checkpointer: BaseCheckpointSaver[Any] | None,
    extensions: ExtensionRegistry,
    *,
    parent: ParentFacts,
    rng: random.Random,
) -> CompiledStateGraph[BaseAgentState, MeivaContext, BaseAgentState, BaseAgentState]:
    # meiva:note
    # Build the self-cycling graph — deps injected via ainvoke(context=MeivaContext);
    #     this function takes the checkpointer and the plugin registry, plus what the process
    #     root read for it: the parent facts every exec child is launched from, and the
    #     generator the llm node's retry jitter and the exec envelope identities are drawn from.
    #
    #     6-Node topology:
    #         START → init_context → claim → before_turn → (llm ↔ exec | harness_turn) → claim
    #
    #     Frontend timeline sync: each node **enter** renders a timeline snapshot
    #     from the in-memory `state.messages` and publishes it via
    #     `node_log.node_lifecycle` before yield (includes msg_count =
    #     `len(state.messages)`); the gateway forwards it to the frontend. Rendering
    #     from in-memory state (not a checkpoint re-read) is race-free: LangGraph
    #     commits checkpoints asynchronously, so a re-read could miss the
    #     just-claimed inbound, but the in-memory state reflects the reducer the
    #     instant it applies. The msg_count protocol lets the frontend distinguish a
    #     single future position (LLM/exec streaming the next message) from a stale
    #     partial via `partial.msg_idx == msg_count`. Fallback (PR #323 streaming
    #     corruption → non-streaming ainvoke) does not resend streaming events;
    #     after commit, the next node enter's snapshot has the committed version,
    #     and the frontend replaces the partial's dirty tokens with the full content
    #     by item_id.
    #
    #     **Routing entirely via Command(goto=)** — the graph build declares a single static edge
    #     add_edge(START, INIT_CONTEXT); business nodes hardcode default next; the hook
    #     container Node accepts a default_next parameter + decides based on update["goto"] override.
    #
    #     Every node gets `input_schema=state_cls` (the class `build_agent_state` returns): LangGraph would
    #     otherwise narrow a node's state to the class its first parameter is annotated with, and that
    #     annotation is the static base class, which carries none of the plugins' channels.
    #
    #     type: ignore[arg-type] — langgraph add_node stub narrows action to the
    #     single-arg StateNode protocol, but runtime accepts the (state, runtime,
    #     config) multi-arg signature. Functionally correct, just stub doesn't narrow.
    #
    # meiva:link agent/graph/docs/graph.md

    state_cls = build_agent_state(extensions)
    g = StateGraph(state_cls, context_schema=MeivaContext)
    process = SubprocessExecProcess(parent, rng)

    async def execute(
        state: BaseAgentState, runtime: Runtime[MeivaContext], config: RunnableConfig
    ) -> Command[ExecGoto]:
        return await exec_node(state, runtime, config, process=process)

    async def llm(
        state: BaseAgentState, runtime: Runtime[MeivaContext], config: RunnableConfig
    ) -> Command[LlmGoto]:
        return await llm_node(state, runtime, config, rng=rng)

    g.add_node(  # type: ignore[arg-type]
        INIT_CONTEXT,
        init_context_node,  # pyright: ignore[reportArgumentType]
        input_schema=state_cls,
        destinations=(CLAIM, BEFORE_TURN, HARNESS_TURN),
    )
    g.add_node(CLAIM, claim_node, input_schema=state_cls)  # type: ignore[arg-type]
    g.add_node(  # type: ignore[arg-type]
        BEFORE_TURN,
        hook_node(  # pyright: ignore[reportArgumentType]
            "before_turn", default_next=LLM, hooks=hooks_at("before_turn", extensions)
        ),
        input_schema=state_cls,
        destinations=(LLM, HARNESS_TURN, CLAIM),
    )
    g.add_node(LLM, llm, input_schema=state_cls)  # type: ignore[arg-type]
    g.add_node(EXEC, execute, input_schema=state_cls)  # type: ignore[arg-type]
    g.add_node(HARNESS_TURN, harness_turn_node, input_schema=state_cls)  # pyright: ignore[reportArgumentType, reportCallIssue, reportUnknownMemberType]
    g.add_edge(START, INIT_CONTEXT)
    if checkpointer is None:
        checkpointer = MemorySaver()
    return g.compile(checkpointer=checkpointer)  # pyright: ignore[reportUnknownMemberType]
