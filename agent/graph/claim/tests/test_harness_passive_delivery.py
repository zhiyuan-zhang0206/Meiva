# meiva:note
# Passive inbox rows must not prevent control claims or bypass non-passive FIFO input.
# meiva:link agent/graph/docs/harness-foreground.md

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.db import claim_inbound_batch
from agent.graph.claim.harness_inbox import INPUT_KEY_FIELD
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    completed,
    input_key,
    insert_row,
    row_status,
    wait_until,
)
from agent.graph.claim.tests.harness_support import harness_runtime as harness_runtime
from agent.graph.claim.tests.harness_support import hosted_resources as hosted_resources
from agent.graph.claim.tests.test_harness_turn import config
from agent.graph.llm_errors import FatalLLMStreamError, HarnessQuotaExhaustedError
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from agent.state import AgentState
from base.agents.recovery.breaker import (
    HALT_REASON_QUOTA_EXHAUSTED,
    QUOTA_RETRY_COUNT_FIELD,
    SUPPRESS_REASON_PERMANENT_REJECT,
)
from base.api_contracts.agent_contract import Harness
from base.db.code_version_gate import ProcessDbGate
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    HarnessSetupError,
    InputInContext,
    Reconciliation,
    TextDelta,
    TurnCompleted,
    TurnOrigin,
    TurnStarted,
    Usage,
)
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records


async def test_the_claim_leaves_deliverable_rows_to_delivery_and_takes_leading_controls(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    control = insert_row(db_conn, agent_id, "resurrect", "system", "")
    chat = insert_row(db_conn, agent_id, "chat", "user", "after the control")
    later = insert_row(db_conn, agent_id, "interrupt", "user", "")
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)

    batch = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )

    assert [item.id for item in batch] == [control], "stops before the first deliverable row"
    assert [row_status(db_conn, i) for i in (chat, later)] == ["pending", "pending"]
    again = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )
    assert again == [], "the chat is next, and it is delivery's"


@pytest.mark.parametrize("kind", ["resurrect", "interrupt", "restart", "terminate"])
@pytest.mark.parametrize("note_tag", ["lifecycle_restart", "lifecycle_interrupt"])
async def test_passive_notes_do_not_block_control_claims(
    kind: str,
    note_tag: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    note = insert_row(
        db_conn,
        agent_id,
        "system_note",
        "system",
        "passive recovery note",
        payload=json.dumps({"passive": True, "note_tag": note_tag}),
    )
    control = insert_row(db_conn, agent_id, kind, "system", "")
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    batch = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )
    assert [item.id for item in batch] == [control]
    assert row_status(db_conn, control) == (
        "claimed" if kind in ("restart", "terminate") else "done"
    )
    assert batch[0].durable_lifecycle == (kind in ("restart", "terminate"))
    assert row_status(db_conn, note) == "pending"


async def test_passive_note_resurrect_chat_claim_leaves_delivery_pending(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
) -> None:
    agent_id = agent_row(db_conn)
    note = insert_row(
        db_conn,
        agent_id,
        "system_note",
        "system",
        "passive recovery note",
        payload='{"passive": true, "note_tag": "lifecycle_restart"}',
    )
    control = insert_row(db_conn, agent_id, "resurrect", "system", "")
    chat = insert_row(db_conn, agent_id, "chat", "user", "continue work")
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    batch = await claim_inbound_batch(
        aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
    )
    assert [item.id for item in batch] == [control]
    assert [row_status(db_conn, i) for i in (note, control, chat)] == ["pending", "done", "pending"]


async def test_non_passive_input_still_blocks_control_behind_a_passive_note(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    chat = insert_row(db_conn, agent_id, "chat", "user", "earlier message")
    note = insert_row(
        db_conn,
        agent_id,
        "system_note",
        "system",
        "passive recovery note",
        payload='{"passive": true, "note_tag": "lifecycle_interrupt"}',
    )
    control = insert_row(db_conn, agent_id, "resurrect", "system", "")
    owner = await admit_owner(aops_pool, agent_id, database_gate=database_gate)
    assert (
        await claim_inbound_batch(
            aops_pool, agent_id, deliverable_by_harness=True, incarnation=owner
        )
        == []
    )
    engine = FakeEngine()
    base = harness_runtime(engine, agent_id=agent_id)
    rt = Runtime(context=replace(base.context, original_incarnation=owner))
    await harness_turn_node(AgentState(), rt, config(agent_id))
    [[sent]] = engine.delivered
    assert sent.key == input_key(chat)
    assert "earlier message" in sent.text and "passive recovery note" in sent.text
    assert [row_status(db_conn, i) for i in (chat, note, control)] == ["done", "done", "pending"]
    batch = await claim_inbound_batch(
        aops_pool,
        agent_id,
        deliverable_by_harness=True,
        incarnation=rt.context.original_incarnation,
    )
    assert [item.id for item in batch] == [control]


def _row_meta(db: psycopg.Connection, inbound_id: int) -> tuple[dict[str, object] | None, object]:
    db.commit()
    row = db.execute(
        "SELECT payload, claimed_at FROM inbound_messages WHERE id = %s",
        (inbound_id,),
    ).fetchone()
    assert row is not None
    return row[0], row[1]


# meiva:note
# The recovery breaker's halt and the wake window: a quota abort halts the agent and writes no window.
def _halt(db: psycopg.Connection, agent_id: int) -> tuple[object, object, object]:
    db.commit()
    row = db.execute(
        "SELECT permanent_reject_streak, last_permanent_reject_reason, wake_suppress_reason "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row is not None
    return row[0], row[1], row[2]


def _assert_requeued_pending(
    db: psycopg.Connection, row_ids: Sequence[int], expected_retries: int
) -> None:
    for row_id in row_ids:
        assert row_status(db, row_id) == "pending"
        payload, claimed_at = _row_meta(db, row_id)
        assert claimed_at is None
        assert payload is not None
        assert INPUT_KEY_FIELD not in payload
        assert payload[QUOTA_RETRY_COUNT_FIELD] == expected_retries


def _error_turn(
    turn_id: str,
    *,
    errors: tuple[str, ...] = (),
    api_error_status: int | None = None,
    error_code: str | None = None,
    subtype: str = "error_during_execution",
) -> TurnCompleted:
    return TurnCompleted(
        turn_id=turn_id,
        session_id=None,
        text="",
        subtype=subtype,
        is_error=True,
        interrupted=False,
        stop_reason=None,
        num_turns=1,
        duration_ms=1,
        usage=Usage(input_tokens=1, output_tokens=0),
        cost_usd=None,
        cumulative_cost_usd=None,
        permission_denials=(),
        api_error_status=api_error_status,
        errors=errors,
        error_code=error_code,
        generation=1,
    )


async def test_quota_abort_after_input_in_context_requeues_rows_and_halts_the_agent(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
    loguru_records: LogRecords,
) -> None:
    agent_id = agent_row(db_conn)
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "please check this")
    note_id = insert_row(
        db_conn,
        agent_id,
        "system_note",
        "system",
        "note context",
        payload='{"note_tag": "sdk_hint"}',
    )
    agy_quota = (
        "Individual quota reached. Please upgrade your subscription to increase your limits. "
        "Resets in 1h24m27s."
    )

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = [item.key for item in inputs]
        return [
            TurnStarted("t1", TurnOrigin.INPUT, tuple(keys), 1),
            *(InputInContext(key, "t1", 1) for key in keys),
            _error_turn("t1", errors=(agy_quota,)),
        ]

    rt = harness_runtime(FakeEngine(script=script), agent_id=agent_id)
    with pytest.raises(HarnessQuotaExhaustedError) as failed:
        await harness_turn_node(AgentState(), rt, config(agent_id))

    exc = failed.value
    assert set(exc.requeued_inbound_ids) == {chat_id, note_id}
    _assert_requeued_pending(db_conn, (chat_id, note_id), expected_retries=1)
    assert _halt(db_conn, agent_id) == (1, HALT_REASON_QUOTA_EXHAUSTED, None)

    quota_logs = loguru_records.extras(event="harness_quota_exhausted", level="WARNING")
    assert len(quota_logs) == 1
    extra = quota_logs[0]
    assert extra["requeued_count"] == 2
    assert "Resets in 1h24m27s" in str(extra["reason"])


async def test_repeated_quota_abort_before_input_in_context_counts_each_attempt(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "retry me carefully")

    def script(_inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        return [
            TurnStarted("t1", TurnOrigin.INPUT, (input_key(chat_id),), 1),
            _error_turn(
                "t1",
                api_error_status=429,
                error_code="rate_limit",
                errors=("You've hit your usage limit",),
            ),
        ]

    for attempt in (1, 2):
        engine = FakeEngine(script=script)
        engine.records = {input_key(chat_id): Reconciliation.UNKNOWN}
        base_rt = harness_runtime(engine, agent_id=agent_id)
        rt = replace(base_rt, context=replace(base_rt.context, harness=Harness.CLAUDE_CODE))

        with pytest.raises(HarnessQuotaExhaustedError) as failed:
            await harness_turn_node(AgentState(), rt, config(agent_id))

        assert failed.value.requeued_inbound_ids == (chat_id,)
        _assert_requeued_pending(db_conn, (chat_id,), expected_retries=attempt)
        assert _halt(db_conn, agent_id) == (attempt, HALT_REASON_QUOTA_EXHAUSTED, None)


async def test_quota_abort_on_setup_error_preserves_unclaimed_deliverables(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "codex quota test")

    class QuotaSetupEngine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            self.delivered.append(list(inputs))
            raise HarnessSetupError("429 Too Many Requests: Try again in 45s")

    engine = QuotaSetupEngine(script=lambda _inputs: [])
    engine.records = {input_key(chat_id): Reconciliation.NOT_FOUND}
    base_rt = harness_runtime(engine, agent_id=agent_id)
    rt = replace(base_rt, context=replace(base_rt.context, harness=Harness.CODEX))

    with pytest.raises(HarnessQuotaExhaustedError) as failed:
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert failed.value.requeued_inbound_ids == (chat_id,)
    assert row_status(db_conn, chat_id) == "pending"
    assert _halt(db_conn, agent_id) == (1, HALT_REASON_QUOTA_EXHAUSTED, None)


async def test_quota_abort_only_requeues_failed_turn_after_earlier_turn_succeeded(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    row1 = insert_row(db_conn, agent_id, "chat", "user", "turn one succeeds")
    row2_holder: list[int] = []

    class TwoTurnEngine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            self.delivered.append(list(inputs))
            keys = [item.key for item in inputs]
            if len(self.delivered) == 1:
                self.emit(
                    TurnStarted("t1", TurnOrigin.INPUT, tuple(keys), 1),
                    *(InputInContext(key, "t1", 1) for key in keys),
                    TextDelta("t1", "MEIVA_DISPLAY_TO_USER\nturn one complete"),
                    completed("t1", "MEIVA_DISPLAY_TO_USER\nturn one complete"),
                )
            else:
                self.emit(
                    TurnStarted("t2", TurnOrigin.INPUT, tuple(keys), 1),
                    *(InputInContext(key, "t2", 1) for key in keys),
                    _error_turn("t2", errors=("Rate limit exceeded. Resets in 0s.",)),
                )

    engine = TwoTurnEngine()
    rt = harness_runtime(engine, agent_id=agent_id)
    node_task = asyncio.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))

    await wait_until(lambda: len(engine.delivered) == 1, "first turn delivered")
    await wait_until(lambda: row_status(db_conn, row1) == "done", "first turn row committed")
    row2 = insert_row(db_conn, agent_id, "chat", "user", "turn two hits quota")
    row2_holder.append(row2)
    engine.notify_inbound()

    with pytest.raises(HarnessQuotaExhaustedError) as failed:
        await asyncio.wait_for(node_task, 20)

    assert failed.value.requeued_inbound_ids == (row2_holder[0],)
    assert row_status(db_conn, row1) == "done"
    assert row_status(db_conn, row2_holder[0]) == "pending"


async def test_non_quota_error_after_input_in_context_stays_done_and_raises_fatal(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "server crash test")

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = [item.key for item in inputs]
        return [
            TurnStarted("t1", TurnOrigin.INPUT, tuple(keys), 1),
            *(InputInContext(key, "t1", 1) for key in keys),
            _error_turn("t1", api_error_status=500, errors=("internal server error",)),
        ]

    rt = harness_runtime(FakeEngine(script=script), agent_id=agent_id)
    with pytest.raises(FatalLLMStreamError) as failed:
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert not isinstance(failed.value, HarnessQuotaExhaustedError)
    assert row_status(db_conn, chat_id) == "done"
    assert _halt(db_conn, agent_id) == (0, None, None)


async def test_quota_abort_adds_to_an_active_permanent_reject_halt_and_keeps_its_window(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "billed account quota fallback")

    # meiva:note
    # reserve_head_batch clears wake_suppressed_until if allowed at admission, so simulate a
    # permanent provider rejection stamped mid-turn before the quota abort settles: the quota abort
    # extends the same halt and leaves the breaker's own window alone.
    class ConcurrentRejectEngine(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            await super().deliver(inputs, compaction_note=compaction_note)
            db_conn.execute(
                "UPDATE agents_meta SET wake_suppress_reason = %s, "
                "wake_suppressed_until = now() + interval '365 days', "
                "permanent_reject_streak = 1 WHERE id = %s",
                (SUPPRESS_REASON_PERMANENT_REJECT, agent_id),
            )
            db_conn.commit()

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = [item.key for item in inputs]
        return [
            TurnStarted("t1", TurnOrigin.INPUT, tuple(keys), 1),
            *(InputInContext(key, "t1", 1) for key in keys),
            _error_turn("t1", api_error_status=429, errors=("rate_limit",)),
        ]

    rt = harness_runtime(ConcurrentRejectEngine(script=script), agent_id=agent_id)
    with pytest.raises(HarnessQuotaExhaustedError):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, chat_id) == "pending"
    assert _halt(db_conn, agent_id) == (
        2,
        HALT_REASON_QUOTA_EXHAUSTED,
        SUPPRESS_REASON_PERMANENT_REJECT,
    )
