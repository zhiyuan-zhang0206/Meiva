import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.execution.protocol import ResultPayload
from agent.graph.exec._child_boundary import ParentFacts
from agent.graph.exec.node import ExecDone, ExecOutputChunkPublisher, ExecResult
from agent.graph.exec.process import read_parent_facts
from base.agents.context import MeivaContext
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.events.live.tests.live_capture import idle_publisher
from base.host.env.agent_slices import AgentSlices
from base.native_process.turn_identity import HostedTurnResources
from base.packages.plugin_providers import build_model_catalog


@dataclass(frozen=True)
class ProcessCall:
    code: str
    context: MeivaContext
    cancel_event: asyncio.Event
    chunks: ExecOutputChunkPublisher
    state: dict[str, Any]
    config_overlay: dict[str, object]


type Outcome = tuple[ExecResult, ResultPayload | None]


# meiva:note
# The exec node's process answering from a script, in call order. It keeps no record of the
# calls: a test that needs to know what reached the process subclasses `answer` and lets the
# outcome carry it, so the assertion reads the node's own result.
class ScriptedExecProcess:
    def __init__(self, outcomes: Sequence[Outcome]) -> None:
        self.outcomes = iter(outcomes)

    def run(
        self,
        code: str,
        context: MeivaContext,
        cancel_event: asyncio.Event,
        chunks: ExecOutputChunkPublisher,
        *,
        state: dict[str, Any],
        config_overlay: dict[str, object],
    ) -> Awaitable[Outcome]:
        call = ProcessCall(code, context, cancel_event, chunks, state, config_overlay)
        return self.answer(call, next(self.outcomes))

    async def answer(self, call: ProcessCall, outcome: Outcome) -> Outcome:
        return outcome


# meiva:note
# Runs no code: each output names the code and the listed state fields the node handed to the
# process, so the tool result the node commits shows what reached it.
class ReportingExecProcess(ScriptedExecProcess):
    def __init__(self, outcomes: Sequence[Outcome], *, report: Sequence[str] = ()) -> None:
        super().__init__(outcomes)
        self.report = tuple(report)

    async def answer(self, call: ProcessCall, outcome: Outcome) -> Outcome:
        result, payload = outcome
        assert isinstance(result, ExecDone)
        seen = " ".join(f"{field}={call.state[field]!r}" for field in self.report)
        return replace(result, output=f"{call.code} | {seen}"), payload


# meiva:note
# This process as a spawning parent, minus the editable-install guard: whether the machine's
# checkout records are healthy is the guard's own tests' subject, not a real child's.
def real_parent(**changes: Any) -> ParentFacts:
    return replace(read_parent_facts(), **{"source_root": None, **changes})


# meiva:note
# A turn context for the exec node. With no pool the turn has no inbound queue and its exec
# runs uninterruptibly, like an eval or container turn; a test that must interrupt passes the
# test database's pool and inserts a real pending row.
def exec_runtime(
    *,
    database_gate: ProcessDbGate,
    ops_pool: AsyncConnectionPool[Any] | None = None,
    hosted_resources: HostedTurnResources | None = None,
    event_publisher: AgentEventPublisher | None = None,
    agent: AgentSlices | None = None,
) -> Runtime[MeivaContext]:
    return Runtime(
        context=MeivaContext(
            hosted_resources=hosted_resources,
            ops_pool=ops_pool,
            event_publisher=event_publisher or idle_publisher(),
            agent=agent or AgentSlices.resolve(default_reader=defaults()),
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(),
            catalog=build_model_catalog(),
            clock_factory=Clock.from_settings,
        )
    )
