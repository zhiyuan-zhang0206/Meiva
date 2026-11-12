# meiva:note
# A scripted turn engine: the graph's view of an external harness, without a process. `deliver()` answers with the events a script produces for that batch, the way a harness reports what became of its inputs. The default script is the common case: the batch starts a turn, every input enters its context, the reply streams, the turn ends.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.agents.history.harness_messages import aload_harness_timeline_items
from base.api_contracts.agent_contract import Harness
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    InputInContext,
    InputRejected,
    Reconciliation,
    ReconciliationUnavailableError,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    Usage,
)

__all__ = [
    "FakeEngine",
    "FakeSubscription",
    "completed",
    "in_a_turn",
    "input_key",
    "insert_row",
    "row_status",
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
        return await self.queue.get()

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
        self.harness = Harness.CLAUDE_CODE
        self.model = "claude-sonnet-5-5"
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
        # meiva:note
        # What a CLI sub-agent did with no turn running, as `take_unattached` hands it over.
        self.unattached_events: list[BridgeEvent] = []

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

    def take_unattached(self) -> list[BridgeEvent]:
        taken, self.unattached_events = self.unattached_events, []
        return taken

    async def subscribe(self) -> FakeSubscription:
        subscription = FakeSubscription(self)
        for event in self.own_turn_events:
            subscription.queue.put_nowait(event)
        self.own_turn_events = []
        self.subscriptions.add(subscription)
        return subscription

    def emit(self, *events: BridgeEvent) -> None:
        for subscription in tuple(self.subscriptions):
            for event in events:
                subscription.queue.put_nowait(event)

    async def deliver(self, inputs: Sequence[HarnessInput], *, compaction_note: str = "") -> None:
        self.delivered.append(list(inputs))
        self.compaction_notes.append(compaction_note)
        self.emit(*self._script(inputs))

    async def reconcile(self, keys: Sequence[str]) -> Mapping[str, Reconciliation]:
        self.reconciled.append(list(keys))
        if self.records is None:
            raise ReconciliationUnavailableError("the fake harness has no readable record")
        return {key: self.records.get(key, Reconciliation.NOT_FOUND) for key in keys}

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
        items = await aload_harness_timeline_items(conn, agent_id)
    return [item.inbound_id for item in items if item.kind == "inbound_chat"]
