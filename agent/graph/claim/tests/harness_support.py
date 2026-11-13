# meiva:note
# A scripted turn engine: the graph's view of an external harness, without a process. `deliver()` answers with the events a script produces for that batch, the way a harness reports what became of its inputs. The default script is the common case: the batch starts a turn, every input enters its context, the reply streams, the turn ends.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import psycopg
import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from base.agents.context import HostedTurnResources, MeivaContext
from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy, format_tool_use
from base.agents.history.transcript_timeline.record.read import read_window
from base.agents.history.transcript_timeline.sent import load_sent_rows, sent_inputs
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.publisher import AgentEventPublisher
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    HarnessCause,
    HarnessInput,
    InputInContext,
    InputRejected,
    ReasoningText,
    Reconciliation,
    ReconciliationUnavailableError,
    ToolResult,
    ToolUse,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    Usage,
)
from base.lm.harness.session.transcript.entries import InputRow
from base.lm.harness.session.transcript.record_file import RecordFile
from base.native_process.turn_identity import HostedServiceResources
from base.packages.composer_commands import CommandCatalog
from base.packages.plugin_providers import build_model_catalog
from base.paths import meiva_home

__all__ = [
    "FakeEngine",
    "FakeSubscription",
    "completed",
    "config",
    "hosted_resources",
    "in_a_turn",
    "input_key",
    "insert_row",
    "read_recorded_items",
    "row_status",
    "runtime",
    "silent_engine",
    "timeline_inbound_ids",
    "wait_until",
]

Script = Callable[[Sequence[HarnessInput]], Sequence[BridgeEvent]]


def completed(
    turn_id: str,
    text: str,
    *,
    usage: Usage | None = None,
    interrupted: bool = False,
    is_error: bool = False,
) -> TurnCompleted:
    return TurnCompleted(
        turn_id=turn_id,
        session_id=None,
        text=text,
        subtype="success",
        is_error=is_error,
        interrupted=interrupted,
        stop_reason=None,
        num_turns=1,
        duration_ms=1,
        usage=usage or Usage(input_tokens=7, output_tokens=3),
        cost_usd=0.01,
        cumulative_cost_usd=None,
        permission_denials=(),
        generation=1,
    )


# meiva:note
# The events of one ordinary turn that took `keys`: it starts, each input enters the context, the
# given activity streams, the turn ends with `reply`.
def in_a_turn(
    keys: Sequence[str],
    reply: str,
    *,
    turn_id: str = "t1",
    activity: Sequence[BridgeEvent] = (),
    usage: Usage | None = None,
    interrupted: bool = False,
) -> list[BridgeEvent]:
    return [
        TurnStarted(turn_id, TurnOrigin.INPUT, tuple(keys), 1),
        *(InputInContext(key, turn_id, 1) for key in keys),
        *activity,
        completed(turn_id, reply, usage=usage, interrupted=interrupted),
    ]


class FakeSubscription:
    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine
        self.queue: asyncio.Queue[BridgeEvent | None] = asyncio.Queue()

    @property
    def unread(self) -> bool:
        return not self.queue.empty()

    async def get(self) -> BridgeEvent | None:
        event = await self.queue.get()
        if self._engine.read_error is not None:
            raise self._engine.read_error
        return event

    def close(self) -> None:
        self._engine.subscriptions.discard(self)


class FakeEngine:
    def __init__(
        self,
        reply: str = "MEIVA_DISPLAY_TO_USER\nthe answer",
        usage: Usage | None = None,
        events: Sequence[BridgeEvent] = (),
        interrupted: bool = False,
        script: Script | None = None,
    ) -> None:
        self.record: AgyRecord | None = None
        self.record_inputs: dict[str, str] = {}
        self.read_error: BaseException | None = None
        self.harness = Harness.ANTIGRAVITY
        self.model = "claude-sonnet-5-5"
        self.session_id: str | None = None
        self.exit_code: int | None = None
        self.delivered: list[list[HarnessInput]] = []
        self.interrupts = 0
        self.retired: list[str] = []
        self.closed = False
        self.generation = 1
        self.context_compacted = False
        self.compaction_notes: list[str] = []
        # meiva:note
        # The harness's durable record as `reconcile` answers it: a key it holds maps to its answer and
        # a key it lacks is NOT_FOUND. None is a record that cannot be read at all.
        self.records: Mapping[str, Reconciliation] | None = None
        self.reconciled: list[list[str]] = []
        self.adopted: list[str] = []
        self.subscriptions: set[FakeSubscription] = set()
        self._inbound = asyncio.Event()
        # meiva:note
        # The events of a turn the CLI began while nothing was subscribed: the next subscription is primed
        # with them, the way the real pump primes it, and the engine reports it as not yet presented.
        self.own_turn_events: list[BridgeEvent] = []

        def ordinary(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
            return in_a_turn(
                [item.key for item in inputs],
                reply,
                activity=events,
                usage=usage,
                interrupted=interrupted,
            )

        self._script: Script = script or ordinary

    @property
    def submitted(self) -> list[tuple[str, str]]:
        return [(i.text, i.key) for batch in self.delivered for i in batch]

    @property
    def has_unpresented_turn(self) -> bool:
        return bool(self.own_turn_events)

    async def subscribe(self) -> FakeSubscription:
        subscription = FakeSubscription(self)
        for event in self.own_turn_events:
            if self.record is not None:
                self.record.observe(event)
            subscription.queue.put_nowait(event)
        self.own_turn_events = []
        self.subscriptions.add(subscription)
        return subscription

    def emit(self, *events: BridgeEvent) -> None:
        if self.record is not None:
            for event in events:
                self.record.observe(event)
        for subscription in tuple(self.subscriptions):
            for event in events:
                subscription.queue.put_nowait(event)

    async def deliver(self, inputs: Sequence[HarnessInput], *, compaction_note: str = "") -> None:
        self.delivered.append(list(inputs))
        if self.record is not None:
            self.record.inputs.update((item.key, item) for item in inputs)
        self.compaction_notes.append(compaction_note)
        self.emit(*self._script(inputs))

    async def reconcile(self, keys: Sequence[str]) -> Mapping[str, Reconciliation]:
        self.reconciled.append(list(keys))
        if self.records is None:
            raise ReconciliationUnavailableError("the fake harness has no readable record")
        answers = {key: self.records.get(key, Reconciliation.NOT_FOUND) for key in keys}
        if self.record is not None:
            for key, answer in answers.items():
                if answer is Reconciliation.IN_CONTEXT and key in self.record_inputs:
                    self.record.confirm_input(key, self.record_inputs[key])
        return answers

    def adopt(self, keys: Sequence[str]) -> None:
        self.adopted.extend(keys)

    async def interrupt(self) -> object:
        self.interrupts += 1
        return object()

    async def retire(self, reason: str) -> None:
        self.retired.append(reason)

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True

    def notify_inbound(self) -> None:
        self._inbound.set()

    async def wait_inbound(self, timeout: float) -> None:
        with suppress(TimeoutError):
            await asyncio.wait_for(self._inbound.wait(), timeout)
        self._inbound.clear()


def rejecting(reason: str = "no active turn to steer") -> Script:
    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        return [InputRejected(i.key, reason, 1) for i in inputs]

    return script


def insert_row(
    db: psycopg.Connection,
    agent_id: int,
    kind: str,
    source: str,
    content: str,
    payload: str | None = None,
) -> int:
    row = db.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
        "VALUES (%s, %s, %s, %s, %s::jsonb) RETURNING id",
        (agent_id, content, kind, source, payload),
    ).fetchone()
    db.commit()
    assert row is not None
    return row[0]


def row_status(db: psycopg.Connection, inbound_id: int) -> object:
    db.commit()
    row = db.execute("SELECT status FROM inbound_messages WHERE id=%s", (inbound_id,)).fetchone()
    assert row is not None
    return row[0]


async def wait_until(condition: Callable[[], bool], what: str) -> None:
    try:
        async with asyncio.timeout(10):
            while not condition():
                await asyncio.sleep(0.02)
    except TimeoutError as error:
        raise AssertionError(what) from error


def silent_engine() -> FakeEngine:
    return FakeEngine(script=lambda _inputs: [])


def input_key(inbound_id: int) -> str:
    return f"inbound-{inbound_id}"


async def timeline_inbound_ids(pool: AsyncConnectionPool, agent_id: int) -> list[int | None]:
    async with pool.connection() as conn:
        items = await read_recorded_items(conn, agent_id)
    return [item.inbound_id for item in items if item.kind == "inbound_chat"]


ENVIRON = dict(os.environ)


def runtime(
    engine: FakeEngine | None,
    *,
    agent_id: int | None = None,
    database_gate: ProcessDbGate | None = None,
    event_bus: EventBus | None = None,
    ops_pool: AsyncConnectionPool | None = None,
    hosted_resources: HostedTurnResources | None = None,
    publisher: AgentEventPublisher | None = None,
) -> Runtime[MeivaContext]:
    if engine is not None and agent_id is not None:
        engine.record = AgyRecord(agent_id, engine.record_inputs)
        engine.session_id = engine.record.session
    return Runtime(
        context=MeivaContext(
            meiva_home=meiva_home(),
            ops_pool=ops_pool,
            bus=event_bus,
            db=Database.from_settings(gate=database_gate) if database_gate is not None else None,
            hosted_resources=hosted_resources,
            harness=Harness.ANTIGRAVITY if engine is not None else Harness.MEIVA,
            harness_bridge=engine,
            llm=MagicMock(),
            event_publisher=publisher or MagicMock(),
            agent=AgentSlices.resolve(default_reader=defaults()),
            catalog=build_model_catalog(home=meiva_home()),
            commands=CommandCatalog.system(skills=list, home=meiva_home()),
            clock_factory=Clock.from_settings,
            environ={**ENVIRON, "HOME": str(meiva_home())},
        )
    )


@pytest.fixture
async def hosted_resources() -> AsyncGenerator[HostedTurnResources]:
    """An explicit service owns the turn's watcher until the test finishes."""
    service = HostedServiceResources()
    try:
        yield await service.turn()
    finally:
        await service.aclose()


def config(agent_id: int = 6984) -> RunnableConfig:
    return {"configurable": {"thread_id": str(agent_id)}}


class AgyRecord:
    def __init__(self, agent_id: int, known_inputs: Mapping[str, str]) -> None:
        self.known_inputs = known_inputs
        self.confirmed: set[str] = set()
        self.session = f"11111111-1111-1111-1111-{agent_id:012d}"
        self.path = _record_path(agent_id)
        self.inputs: dict[str, HarnessInput] = {}
        self.planner_text: dict[str, str] = {}

    def write(self, kind: str, **fields: object) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            _ = handle.write(
                json.dumps(
                    {
                        "type": kind,
                        "status": "DONE",
                        "created_at": datetime.now(UTC).isoformat(),
                        "step_index": 1,
                        **fields,
                    }
                )
                + "\n"
            )

    def confirm_input(self, key: str, text: str) -> None:
        if key not in self.confirmed:
            self.confirmed.add(key)
            self.write("USER_INPUT", content=f"<USER_REQUEST>{text}</USER_REQUEST>")

    def observe(self, event: BridgeEvent) -> None:
        if isinstance(event, InputInContext):
            item = self.inputs.get(event.key)
            if item is not None:
                self.confirm_input(event.key, item.text)
            elif event.key in self.known_inputs:
                self.confirm_input(event.key, self.known_inputs[event.key])
        elif isinstance(event, (ToolUse, ToolResult)):
            self._tool(event)
        elif isinstance(event, ReasoningText):
            self.write("PLANNER_RESPONSE", thinking=event.text)
        elif isinstance(event, AssistantText):
            self.planner_text[event.turn_id] = event.text
            if event.text:
                self.write("PLANNER_RESPONSE", content=event.text)
        elif isinstance(event, TurnCompleted):
            previous = self.planner_text.pop(event.turn_id, None)
            text = event.text if previous is None else ""
            if text:
                self.write("PLANNER_RESPONSE", content=text)
        elif isinstance(event, TurnStarted):
            self._started(event)

    def _tool(self, event: ToolUse | ToolResult) -> None:
        if isinstance(event, ToolUse):
            if event.parent_tool_use_id is None:
                self.write(
                    "PLANNER_RESPONSE", tool_calls=[{"name": event.name, "args": event.tool_input}]
                )
        elif isinstance(event, ToolResult) and event.parent_tool_use_id is None:
            self.write(
                "GENERIC",
                content=event.content,
                status="ERROR" if event.is_error else "DONE",
                error=event.content if event.is_error else "",
            )

    def _started(self, event: TurnStarted) -> None:
        if event.origin is not TurnOrigin.INPUT:
            sender = (
                "system"
                if event.cause in (HarnessCause.QUEUED_INPUT_REVIVED, HarnessCause.UNKNOWN)
                else "fake/task-1"
            )
            self.write(
                "SYSTEM_MESSAGE",
                content=f"<SYSTEM_MESSAGE>\n[Message] timestamp=t sender={sender} priority=normal content=Synthetic background wake\n</SYSTEM_MESSAGE>",
            )


def _record_path(agent_id: int) -> Path:
    session = f"11111111-1111-1111-1111-{agent_id:012d}"
    return (
        meiva_home()
        / ".gemini/antigravity-cli/brain"
        / session
        / ".system_generated/logs/transcript_full.jsonl"
    )


async def read_recorded_items(
    conn: psycopg.AsyncConnection,
    agent_id: int,
    *,
    policy: ReplyPolicy | None = None,
) -> list[TimelineItem]:
    path = _record_path(agent_id)
    if not path.is_file():
        return []
    with psycopg.connect(conn.info.dsn) as db:

        def load(until: datetime, wanted: Mapping[str, InputRow]) -> dict[str, list[TimelineItem]]:
            return sent_inputs(
                load_sent_rows(
                    db, agent_id, until=until, wanted=wanted, wire_id=lambda key: key
                ).rows
            )

        return read_window(
            RecordFile(path),
            harness="antigravity",
            limit=1000,
            before=None,
            policy=policy if policy is not None else ReplyPolicy(True, "prefix"),
            render_tool=format_tool_use,
            load_sent=load,
        ).timeline_items


@dataclass(frozen=True)
class RuntimeFixture:
    pool: AsyncConnectionPool
    resources: HostedTurnResources
    gate: ProcessDbGate
    bus: EventBus

    def __call__(
        self,
        engine: FakeEngine | None,
        *,
        agent_id: int,
        publisher: AgentEventPublisher | None = None,
    ) -> Runtime[MeivaContext]:
        return runtime(
            engine,
            agent_id=agent_id,
            database_gate=self.gate,
            event_bus=self.bus,
            ops_pool=self.pool,
            hosted_resources=self.resources,
            publisher=publisher,
        )


@pytest.fixture
async def harness_runtime(
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> RuntimeFixture:
    return RuntimeFixture(aops_pool, hosted_resources, database_gate, event_bus)
