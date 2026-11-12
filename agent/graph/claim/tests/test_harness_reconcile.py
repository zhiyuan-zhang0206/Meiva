# meiva:note
# Reconciliation of inputs handed to an external harness and never acknowledged: what a restart, an
# abort or a crash leaves claimed is judged against the harness's own durable record. In the context
# settles it, absent returns it to the queue to be delivered again in order, stored-but-undelivered
# stays claimed for the harness to deliver itself, and an unreadable record is never guessed at.
# meiva:link agent/graph/docs/harness-foreground.md

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_foreground import (
    Commit,
    Foreground,
    Handed,
    plan_reconciliation,
)
from agent.graph.claim.harness_inbox import claimed_deliverable, recovery_notes_first
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.node import claim_node
from agent.graph.claim.tests.claim_support import make_runtime
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    completed,
    in_a_turn,
    input_key,
    insert_row,
    row_status,
    silent_engine,
    wait_until,
)
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.graph.llm_errors import FatalLLMStreamError
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.agents.messages.lifecycle_text import interrupt_note
from base.api_contracts.agent_contract import Harness
from base.db.code_version_gate import ProcessDbGate
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    InputInContext,
    Reconciliation,
    SessionClosed,
    TextDelta,
    TurnOrigin,
    TurnStarted,
)
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.native_process.turn_identity import HostedTurnResources


def h_insert(db: psycopg.Connection, agent_id: int, content: str, *, claimed: bool = False) -> int:
    row = db.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, status, claimed_at) "
        "VALUES (%s, %s, 'chat', 'user', %s, CASE WHEN %s THEN now() END) RETURNING id",
        (agent_id, content, "claimed" if claimed else "pending", claimed),
    ).fetchone()
    db.commit()
    assert row is not None
    return row[0]


def h_status(db: psycopg.Connection, inbound_id: int) -> object:
    db.commit()
    row = db.execute("SELECT status FROM inbound_messages WHERE id=%s", (inbound_id,)).fetchone()
    assert row is not None
    return row[0]


def h_key(inbound_id: int) -> str:
    return f"inbound-{inbound_id}"


async def h_until(condition: object, what: str) -> None:
    assert callable(condition)
    try:
        async with asyncio.timeout(10):
            while not condition():
                await asyncio.sleep(0.02)
    except TimeoutError as error:
        raise AssertionError(what) from error


async def h_timeline(pool: AsyncConnectionPool, agent_id: int) -> list[tuple[str, int | None, str]]:
    async with pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, agent_id)
    return [(item.kind, item.inbound_id, item.payload) for item in items]


async def h_deferred(db: psycopg.Connection, inbound_id: int) -> bool:
    db.commit()
    row = db.execute(
        "SELECT payload->>'harness_reconcile_deferred' FROM inbound_messages WHERE id=%s",
        (inbound_id,),
    ).fetchone()
    assert row is not None
    return row[0] == "true"


def h_silent() -> FakeEngine:
    return FakeEngine(script=lambda _inputs: [])


async def test_a_claimed_input_the_harness_has_in_context_is_settled_and_the_agent_is_told_to_go_on(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "seen before the crash", claimed=True)
    engine = FakeEngine()
    engine.records = {h_key(row): Reconciliation.IN_CONTEXT}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    [[note]] = engine.delivered
    assert note.key == f"reconciled-resume-{row}", "the input is not sent again, only a note"
    assert "restarted" in note.text and "interrupted" in note.text
    assert h_status(db_conn, row) == "done"
    assert [i for k, i, _ in await h_timeline(aops_pool, agent_id) if k == "inbound_chat"] == [row]


async def test_a_claimed_input_the_harness_stored_is_adopted_nudged_and_asked_about_once_more(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "queued inside the CLI", claimed=True)
    engine = FakeEngine()
    engine.records = {h_key(row): Reconciliation.PERSISTED_UNDELIVERED}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert engine.adopted == [h_key(row)]
    assert [i.key for batch in engine.delivered for i in batch] == [f"reconciled-resume-{row}"], (
        "a stdin input is what makes the CLI deliver its own queue; the stored input is not sent"
    )
    assert engine.reconciled == [[h_key(row)], [h_key(row)]], (
        "asked again once the nudge turn ended"
    )
    assert h_status(db_conn, row) == "claimed", "still undelivered, so it is left alone"
    assert await h_deferred(db_conn, row), "and marked so it does not wake the agent again"


async def test_a_stored_input_that_appears_while_the_agent_works_is_settled_by_its_own_fact(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "queued inside the CLI", claimed=True)

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = [i.key for i in inputs]
        return in_a_turn([*keys, h_key(row)], "MEIVA_DISPLAY_TO_USER\nok")

    engine = FakeEngine(script=script)
    engine.records = {h_key(row): Reconciliation.PERSISTED_UNDELIVERED}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert engine.reconciled == [[h_key(row)]], "settled by the event, nothing left to ask about"
    assert h_status(db_conn, row) == "done"
    assert [i for k, i, _ in await h_timeline(aops_pool, agent_id) if k == "inbound_chat"] == [row]


async def test_a_stored_input_the_cli_dropped_goes_back_when_the_second_question_finds_it_gone(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "queued inside the CLI", claimed=True)

    class Dropping(FakeEngine):
        async def reconcile(self, keys: Sequence[str]) -> Mapping[str, Reconciliation]:
            answer = (
                Reconciliation.PERSISTED_UNDELIVERED
                if len(self.reconciled) == 0
                else Reconciliation.NOT_FOUND
            )
            self.reconciled.append(list(keys))
            return dict.fromkeys(keys, answer)

    engine = Dropping()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert [i.key for i in engine.delivered[-1]] == [h_key(row)], "delivered again, in this run"
    assert h_status(db_conn, row) == "done"


async def test_a_claimed_input_the_harness_never_saw_goes_back_and_is_delivered_in_order(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    lost = h_insert(db_conn, agent_id, "never reached the CLI", claimed=True)
    newer = h_insert(db_conn, agent_id, "arrived while the host was down")
    engine = FakeEngine()
    engine.records = {}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    [batch] = engine.delivered
    [one] = batch
    assert one.key == h_key(lost)
    assert one.text.index("never reached the CLI") < one.text.index(
        "arrived while the host was down"
    ), "its original position"
    assert [h_status(db_conn, i) for i in (lost, newer)] == ["done", "done"]


async def test_a_batch_of_claimed_inputs_is_judged_one_by_one_and_redelivered_in_order(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    seen = h_insert(db_conn, agent_id, "in the context", claimed=True)
    lost = h_insert(db_conn, agent_id, "not in the record", claimed=True)
    stored = h_insert(db_conn, agent_id, "in the CLI's own queue", claimed=True)
    unknown = h_insert(db_conn, agent_id, "the record cannot tell", claimed=True)
    newer = h_insert(db_conn, agent_id, "queued while down")
    engine = FakeEngine()
    engine.records = {
        h_key(seen): Reconciliation.IN_CONTEXT,
        h_key(stored): Reconciliation.PERSISTED_UNDELIVERED,
        h_key(unknown): Reconciliation.UNKNOWN,
    }
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    [batch] = engine.delivered
    [one] = batch
    assert one.key == h_key(lost), "the resume note rides the wake's one input"
    assert one.text.index("restarted") < one.text.index("not in the record")
    assert "queued while down" in one.text
    assert engine.adopted == [h_key(stored)]
    assert [h_status(db_conn, i) for i in (seen, lost, unknown, newer)] == ["done"] * 4
    assert h_status(db_conn, stored) == "claimed"
    timeline = await h_timeline(aops_pool, agent_id)
    assert [i for _, i, p in timeline if "could not be confirmed" in p] == [unknown]


async def test_an_unreadable_record_is_never_guessed_at_and_the_marker_is_written(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "unknown fate", claimed=True)
    engine = h_silent()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert engine.reconciled == [[h_key(row)]]
    assert engine.delivered == []
    assert h_status(db_conn, row) == "done"
    assert any(
        "could not be confirmed" in text for _, _, text in await h_timeline(aops_pool, agent_id)
    )


async def test_the_recovery_note_is_delivered_before_the_messages_that_queued_up(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    lost = h_insert(db_conn, agent_id, "claimed before the restart", claimed=True)
    _ = h_insert(db_conn, agent_id, "queued during the restart")
    state = AgentState(
        harness_inputs=[{"key": "control-9", "prompt": "you were restarted", "timeline_items": []}]
    )
    engine = FakeEngine()
    engine.records = {}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(state, rt, config(agent_id))

    [batch] = engine.delivered
    [one] = batch
    assert one.key == h_key(lost)
    assert one.text.index("you were restarted") < one.text.index("claimed before the restart")
    assert "queued during the restart" in one.text


async def test_a_frame_of_an_earlier_generation_does_not_settle_the_current_generations_input(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "delivered again after a respawn")
    engine = h_silent()
    engine.generation = 2
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await h_until(lambda: bool(engine.delivered), "delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (h_key(row),), 1),
            InputInContext(h_key(row), "t1", 1),
        )
        await asyncio.sleep(0.3)
        assert h_status(db_conn, row) == "claimed", "the old process's fact is not the new one's"
        assert not node.done()
        engine.emit(InputInContext(h_key(row), "t1", 2))
        await node

    assert h_status(db_conn, row) == "done"


# meiva:note
# The delta carries a number where text belongs, which no harness sends: it is how a test makes the
# projector fail, so the failure path of an invocation can be observed.
# meiva:link agent/graph/docs/harness-foreground.md
@pytest.mark.parametrize(
    ("answer", "status", "in_timeline"),
    [
        (Reconciliation.IN_CONTEXT, "done", True),
        (Reconciliation.NOT_FOUND, "pending", False),
        (Reconciliation.PERSISTED_UNDELIVERED, "claimed", False),
    ],
)
async def test_a_failed_invocation_reconciles_what_it_had_handed_over(
    answer: Reconciliation,
    status: str,
    in_timeline: bool,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "work")
    engine = h_silent()
    engine.records = {h_key(row): answer} if answer is not Reconciliation.NOT_FOUND else {}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    node = asyncio.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
    await h_until(lambda: bool(engine.delivered), "delivery")
    engine.emit(TextDelta("t1", 5))  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        await asyncio.wait_for(node, 20)

    assert engine.retired, "the session is retired first, so the CLI cannot take the input later"
    assert h_status(db_conn, row) == status
    timeline = await h_timeline(aops_pool, agent_id)
    assert (row in [i for k, i, _ in timeline if k == "inbound_chat"]) is in_timeline
    assert not any("could not be confirmed" in text for _, _, text in timeline)


# meiva:note
# The delta carries a number where text belongs, which no harness sends: it is how a test makes the
# projector fail, so the failure path of an invocation can be observed.
# meiva:link agent/graph/docs/harness-foreground.md
async def test_a_session_that_cannot_be_retired_falls_back_to_the_marker(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "work")

    class Stuck(FakeEngine):
        async def retire(self, reason: str) -> None:
            raise RuntimeError("the CLI would not close")

    engine = Stuck(script=lambda _inputs: [])
    engine.records = {}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    node = asyncio.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
    await h_until(lambda: bool(engine.delivered), "delivery")
    engine.emit(TextDelta("t1", 5))  # pyright: ignore[reportArgumentType]
    with pytest.raises((TypeError, FatalLLMStreamError)):
        await asyncio.wait_for(node, 20)

    assert engine.reconciled == [], "an input is judged absent only once the CLI is certainly gone"
    assert h_status(db_conn, row) == "done"
    assert any(
        "could not be confirmed" in text for _, _, text in await h_timeline(aops_pool, agent_id)
    )


@pytest.mark.parametrize("orphaned", [True, False])
async def test_the_claim_enters_the_turn_for_an_orphaned_claim_even_with_nothing_queued(
    orphaned: bool,
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    if orphaned:
        _ = h_insert(db_conn, agent_id, "handed over before the restart", claimed=True)
    base = make_runtime(ops_pool=aops_pool, database_gate=database_gate)
    ctx = replace(
        base.context,
        harness=Harness.CLAUDE_CODE,
        harness_bridge=h_silent(),
        hosted_resources=hosted_resources,
    )

    entry = await claim_node(AgentState(halted=True), Runtime(context=ctx), config(agent_id))

    assert (entry.goto == "before_turn") is orphaned, "no message has to arrive to reconcile it"


def _handed(key: str, inbound: int | None = 1) -> Handed:
    return Handed(key, inbound, ())


def test_the_plan_splits_inputs_by_what_the_record_says_and_keeps_their_order() -> None:
    handed = [_handed("a"), _handed("b"), _handed("c"), _handed("d")]
    plan = plan_reconciliation(
        handed,
        {
            "a": Reconciliation.NOT_FOUND,
            "b": Reconciliation.IN_CONTEXT,
            "c": Reconciliation.PERSISTED_UNDELIVERED,
            "d": Reconciliation.NOT_FOUND,
        },
    )

    assert [h.key for h in plan.requeue] == ["a", "d"]
    assert [h.key for h in plan.commit] == ["b"]
    assert [h.key for h in plan.keep] == ["c"]


def test_a_missing_answer_is_an_error_not_a_default() -> None:
    with pytest.raises(ValueError, match="no reconciliation answer"):
        _ = plan_reconciliation([_handed("a")], {})


def test_an_event_of_an_earlier_generation_is_stale_and_settles_nothing() -> None:
    fg = Foreground(generation=2).hand((_handed("a"),))

    stale, nothing = fg.on_event(InputInContext("a", "t1", 1))
    current, settled = fg.on_event(InputInContext("a", "t1", 2))

    assert stale is fg and nothing == ()
    assert [type(s) for s in settled] == [Commit]
    assert current.handed == ()


def test_a_frame_that_names_no_generation_is_never_judged_stale() -> None:
    fg = Foreground(generation=3)

    assert not fg.is_stale(InputInContext("a", "t1", 0))
    assert fg.is_stale(InputInContext("a", "t1", 2))
    assert not fg.is_stale(InputInContext("a", "t1", 3))


class Unreadable(FakeEngine):
    async def reconcile(self, keys: Sequence[str]) -> Mapping[str, Reconciliation]:
        raise OSError("the record could not be read")


async def test_a_record_that_exists_but_cannot_be_read_leaves_the_rows_for_the_next_wake(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    first = h_insert(db_conn, agent_id, "one", claimed=True)
    second = h_insert(db_conn, agent_id, "two", claimed=True)
    rt = runtime(Unreadable(), ops_pool=aops_pool, hosted_resources=hosted_resources)

    with pytest.raises(OSError, match="could not be read"):
        async with asyncio.timeout(20):
            await harness_turn_node(AgentState(), rt, config(agent_id))

    assert [h_status(db_conn, i) for i in (first, second)] == ["claimed", "claimed"]
    assert not any(
        "could not be confirmed" in p for _, _, p in await h_timeline(aops_pool, agent_id)
    )


# meiva:note
# The delta carries a number where text belongs, which no harness sends: it is how a test makes the
# projector fail, so the failure path of an invocation can be observed.
# meiva:link agent/graph/docs/harness-foreground.md
async def test_a_failed_run_whose_record_cannot_be_read_keeps_its_rows_and_its_own_error(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "work")
    engine = Unreadable(script=lambda _inputs: [])
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    node = asyncio.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
    await h_until(lambda: bool(engine.delivered), "delivery")
    engine.emit(TextDelta("t1", 5))  # pyright: ignore[reportArgumentType]
    with pytest.raises(TypeError):
        await asyncio.wait_for(node, 20)

    assert h_status(db_conn, row) == "claimed", "reconciled at the next wake, not consumed"


@pytest.mark.parametrize(
    ("records", "named"),
    [({}, False), (None, True)],
    ids=["returned to the queue", "consumed with the marker"],
)
async def test_the_lost_turn_note_names_only_what_was_consumed_with_the_marker(
    records: dict[str, Reconciliation] | None,
    named: bool,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    loguru_records: LogRecords,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "work")
    engine = h_silent()
    engine.records = records
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    node = asyncio.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
    await h_until(lambda: bool(engine.delivered), "delivery")
    engine.emit(SessionClosed("the CLI went away"))
    with pytest.raises(FatalLLMStreamError):
        await asyncio.wait_for(node, 20)

    reported = [r["extra"]["inbound_ids"] for r in loguru_records.events("turn_fault_lost_inbound")]
    assert reported == ([[row]] if named else []), "only a consumed row is reported as lost"
    assert h_status(db_conn, row) == ("done" if named else "pending")


async def test_a_restart_note_goes_ahead_of_the_messages_that_were_returned_to_the_queue(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    _ = h_insert(db_conn, agent_id, "claimed before the restart", claimed=True)
    _ = h_insert(db_conn, agent_id, "queued while it restarted")
    row = db_conn.execute(
        "INSERT INTO inbound_messages (agent_id, content, kind, source, payload) "
        "VALUES (%s, 'you were restarted', 'system_note', 'system', "
        "jsonb_build_object('note_tag', 'lifecycle_restart')) RETURNING id",
        (agent_id,),
    ).fetchone()
    db_conn.commit()
    assert row is not None
    note = row[0]
    engine = FakeEngine()
    engine.records = {}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    [batch] = engine.delivered
    [one] = batch
    assert one.key == h_key(note)
    assert (
        one.text.index("you were restarted")
        < one.text.index("claimed before the restart")
        < one.text.index("queued while it restarted")
    ), "the agent learns it was restarted before it sees the messages that are delivered again"


async def test_a_row_the_harness_still_has_undelivered_is_left_to_the_next_real_wake(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "stored by the CLI", claimed=True)
    db_conn.execute(
        "UPDATE inbound_messages SET payload=jsonb_build_object('harness_reconcile_deferred', true) "
        "WHERE id=%s",
        (row,),
    )
    db_conn.commit()
    engine = FakeEngine()
    engine.records = {h_key(row): Reconciliation.PERSISTED_UNDELIVERED}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    assert await claimed_deliverable(rt.context, agent_id, include_deferred=False) == []
    assert [
        r.id for r in await claimed_deliverable(rt.context, agent_id, include_deferred=True)
    ] == [row]
    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert engine.delivered == [], "no nudge and no wait: it was already asked about"
    assert engine.adopted == []
    assert h_status(db_conn, row) == "claimed"


async def test_a_deferred_row_is_settled_when_a_later_run_finds_it_in_the_context(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    row = h_insert(db_conn, agent_id, "stored by the CLI", claimed=True)
    db_conn.execute(
        "UPDATE inbound_messages SET payload=jsonb_build_object('harness_reconcile_deferred', true) "
        "WHERE id=%s",
        (row,),
    )
    db_conn.commit()
    engine = FakeEngine()
    engine.records = {h_key(row): Reconciliation.IN_CONTEXT}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert h_status(db_conn, row) == "done"


def test_lifecycle_notes_go_first_and_everything_else_keeps_its_order() -> None:
    from agent.db import ClaimedInbound

    def row(inbound_id: int, kind: str, tag: str | None = None) -> ClaimedInbound:
        return ClaimedInbound(inbound_id, 1, "x", kind, "user", {"note_tag": tag} if tag else None)

    rows = [
        row(1, "chat"),
        row(2, "system_note", "agent_reply"),
        row(3, "system_note", "lifecycle_restart"),
        row(4, "chat"),
        row(5, "system_note", "lifecycle_resurrect"),
    ]

    assert [r.id for r in recovery_notes_first(rows)] == [3, 5, 1, 2, 4]


# meiva:note
# The three rows an interrupt commits reach the harness as the note then the message, and the control
# row, last in the queue, stays pending behind them until claim takes it.
async def test_an_interrupts_note_and_message_are_delivered_in_order_ahead_of_its_control_row(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    first = insert_row(db_conn, agent_id, "chat", "user", "work")
    engine = silent_engine()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)
    note_text = interrupt_note("agent:7", with_message=True)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: len(engine.delivered) == 1, "first delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (input_key(first),), 1),
            InputInContext(input_key(first), "t1", 1),
        )
        note = insert_row(
            db_conn,
            agent_id,
            "system_note",
            "agent:7",
            note_text,
            payload='{"note_tag": "lifecycle_interrupt"}',
        )
        message = insert_row(db_conn, agent_id, "chat", "agent:7", "do Y instead")
        control = insert_row(db_conn, agent_id, "interrupt", "agent:7", "")
        engine.notify_inbound()
        await wait_until(lambda: len(engine.delivered) == 2, "the note and message are delivered")
        await wait_until(lambda: engine.interrupts == 1, "the control row interrupts")
        engine.emit(
            completed("t1", "", interrupted=True),
            TurnStarted("t2", TurnOrigin.HARNESS, (input_key(note), input_key(message)), 1),
            InputInContext(input_key(note), "t2", 1),
            InputInContext(input_key(message), "t2", 1),
            completed("t2", "MEIVA_DISPLAY_TO_USER\nhandled"),
        )
        await node

    delivered = [i for batch in engine.delivered[1:] for i in batch]
    assert [i.key for i in delivered] == [input_key(note)]
    assert delivered[0].text.index(note_text) < delivered[0].text.index("do Y instead")
    assert row_status(db_conn, control) == "pending", "claim takes the control row next"


# meiva:note
# The stop button on an external harness leaves a passive note and a control row. The interrupt ends
# the turn, and the passive note is not handed to the harness: a delivery would start a turn of the
# harness's own for a note that asked for none. It waits for the next real input.
async def test_the_stop_buttons_passive_note_does_not_start_a_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    first = insert_row(db_conn, agent_id, "chat", "user", "work")
    engine = silent_engine()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: len(engine.delivered) == 1, "first delivery")
        engine.emit(
            TurnStarted("t1", TurnOrigin.INPUT, (input_key(first),), 1),
            InputInContext(input_key(first), "t1", 1),
        )
        control = insert_row(db_conn, agent_id, "interrupt", "user", "")
        note = insert_row(
            db_conn,
            agent_id,
            "system_note",
            "user",
            "Your running turn was interrupted by user.",
            payload='{"note_tag": "lifecycle_interrupt", "passive": true}',
        )
        engine.notify_inbound()
        await wait_until(lambda: engine.interrupts == 1, "the control row interrupts")
        engine.emit(completed("t1", "", interrupted=True))
        await node

    assert len(engine.delivered) == 1, "the passive note started no turn"
    assert row_status(db_conn, note) == "pending"
    assert row_status(db_conn, control) == "pending", "claim takes the control row next"


# meiva:note
# A passive note waits for the next real input and goes along with it, ahead of it, in queue order.
async def test_a_passive_note_goes_along_with_the_next_real_input(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
) -> None:
    agent_id = agent_row(db_conn)
    _ = insert_row(
        db_conn,
        agent_id,
        "system_note",
        "user",
        "Your running turn was interrupted by user.",
        payload='{"note_tag": "lifecycle_interrupt", "passive": true}',
    )
    chat = insert_row(db_conn, agent_id, "chat", "user", "carry on")
    engine = silent_engine()
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        while not engine.delivered:
            await asyncio.sleep(0.02)
        keys = [i.key for i in engine.delivered[0]]
        text = engine.delivered[0][0].text
        node.cancel()
    assert keys == [f"inbound-{chat}"], "the passive note rides the message and opens no turn"
    assert text.index("was interrupted") < text.index("carry on")
