# meiva:note
# Passive inbox rows must not prevent control claims or bypass non-passive FIFO input. A harness turn
# that stops without a reply (an exhausted quota, a stalled turn) hands its rows back and halts the
# agent, and a handed-back row is handed over again under a key the harness has not recorded.
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
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    completed,
    in_a_turn,
    input_key,
    insert_row,
    row_status,
    wait_until,
)
from agent.graph.claim.tests.harness_support import harness_runtime as harness_runtime
from agent.graph.claim.tests.harness_support import hosted_resources as hosted_resources
from agent.graph.claim.tests.test_harness_turn import config
from agent.graph.llm_errors import FatalLLMStreamError, HarnessTurnHaltedError
from agent.ownership.tests.ownership_support import admit_owner, agent_row
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.agents.messages.harness_input_key import (
    HANDBACK_COUNT_FIELD,
    HANDED_KEYS_FIELD,
    INPUT_KEY_FIELD,
)
from base.agents.recovery.breaker import (
    HALT_REASON_QUOTA_EXHAUSTED,
    HALT_REASON_TURN_STALLED,
    SUPPRESS_REASON_PERMANENT_REJECT,
)
from base.api_contracts.agent_contract import Harness
from base.config.tests.configuration import defaults
from base.db.code_version_gate import ProcessDbGate
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    HarnessInput,
    HarnessSetupError,
    InputInContext,
    Reconciliation,
    TextDelta,
    ToolResult,
    ToolUse,
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
        assert payload[HANDBACK_COUNT_FIELD] == expected_retries


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
    with pytest.raises(HarnessTurnHaltedError) as failed:
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

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        return [
            TurnStarted("t1", TurnOrigin.INPUT, tuple(item.key for item in inputs), 1),
            _error_turn(
                "t1",
                api_error_status=429,
                error_code="rate_limit",
                errors=("You've hit your usage limit",),
            ),
        ]

    keys = {1: input_key(chat_id), 2: f"{input_key(chat_id)}-handback-1"}
    for attempt in (1, 2):
        engine = FakeEngine(script=script)
        engine.records = {keys[attempt]: Reconciliation.UNKNOWN}
        base_rt = harness_runtime(engine, agent_id=agent_id)
        rt = replace(base_rt, context=replace(base_rt.context, harness=Harness.CLAUDE_CODE))

        with pytest.raises(HarnessTurnHaltedError) as failed:
            await harness_turn_node(AgentState(), rt, config(agent_id))

        assert [key for _text, key in engine.submitted] == [keys[attempt]]
        assert failed.value.reason == HALT_REASON_QUOTA_EXHAUSTED
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

    with pytest.raises(HarnessTurnHaltedError) as failed:
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

    with pytest.raises(HarnessTurnHaltedError) as failed:
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

    assert not isinstance(failed.value, HarnessTurnHaltedError)
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
    with pytest.raises(HarnessTurnHaltedError):
        await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, chat_id) == "pending"
    assert _halt(db_conn, agent_id) == (
        2,
        HALT_REASON_QUOTA_EXHAUSTED,
        SUPPRESS_REASON_PERMANENT_REJECT,
    )


# meiva:note
# Task #7423, 2026-11-19: rows a quota abort returned to the queue were handed to the resumed Claude
# Code CLI under the key they first had, so under a uuid the CLI already held. The CLI echoes such a
# message and then drops it: no turn, no result. The foreground waited for the turn forever, the
# agent showed as running, and everything queued behind it was stuck. `DedupingEngine` answers the
# way that CLI does: an input under a key it has not recorded starts a turn (the first one runs out
# of quota when `quota_first`); an input under a key it recorded is echoed and nothing follows.
class DedupingEngine(FakeEngine):
    def __init__(self, *, quota_first: bool = False) -> None:
        super().__init__(script=self._answer)
        self.recorded: set[str] = set()
        self._quota_first = quota_first
        self._turns = 0

    def _answer(self, inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = [item.key for item in inputs]
        self._turns += 1
        turn_id = f"t{self._turns}"
        echo = [
            TurnStarted(turn_id, TurnOrigin.INPUT, tuple(keys), 1),
            *(InputInContext(key, turn_id, 1) for key in keys),
        ]
        if any(key in self.recorded for key in keys):
            return echo
        self.recorded.update(keys)
        if self._quota_first and self._turns == 1:
            limit = "You've hit your weekly limit · resets Nov 23 at 2pm (UTC)"
            quota = _error_turn(
                turn_id, api_error_status=429, error_code="rate_limit", errors=(limit,)
            )
            return [*echo, quota]
        return in_a_turn(keys, "MEIVA_HIDE_FROM_USER\ndone", turn_id=turn_id)


def _with_stall_seconds(rt: Runtime[MeivaContext], seconds: float) -> Runtime[MeivaContext]:
    agent = AgentSlices.resolve({"harness_turn_stall_seconds": seconds}, default_reader=defaults())
    return replace(rt, context=replace(rt.context, agent=agent))


# meiva:note
# The incident at the foreground: the input enters the context, the quota runs out, the row goes back
# to the queue; the next run (a resurrect, a new message, or the ordinary delivery loop taking the row
# again) hands it over under a key the CLI has not recorded, so the CLI runs a turn. Under the old key
# the second run stalls instead.
async def test_a_handed_back_row_is_handed_over_under_a_new_key_and_starts_a_turn(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "check the release")
    engine = DedupingEngine(quota_first=True)
    rt = _with_stall_seconds(harness_runtime(engine, agent_id=agent_id), 5.0)

    with pytest.raises(HarnessTurnHaltedError) as halted:
        await harness_turn_node(AgentState(), rt, config(agent_id))
    assert halted.value.reason == HALT_REASON_QUOTA_EXHAUSTED
    _assert_requeued_pending(db_conn, (chat_id,), expected_retries=1)
    payload, _claimed_at = _row_meta(db_conn, chat_id)
    assert payload is not None and payload[HANDED_KEYS_FIELD] == [input_key(chat_id)]

    later = insert_row(db_conn, agent_id, "chat", "user", "the quota is reset, go on")
    _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    first, second = engine.delivered
    assert [item.key for item in first] == [input_key(chat_id)]
    assert [item.key for item in second] == [f"{input_key(chat_id)}-handback-1"]
    assert [row_status(db_conn, i) for i in (chat_id, later)] == ["done", "done"]


# meiva:note
# Whatever leaves a turn with nothing more to say (here the same dropped input), no event within
# `harness_turn_stall_seconds` ends it: the session is retired, the inputs go back to the queue
# counted, the agent halts with `turn_stalled`, and the warning names it.
async def test_a_turn_with_no_event_past_the_threshold_halts_and_hands_its_inputs_back(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
    loguru_records: LogRecords,
) -> None:
    agent_id = agent_row(db_conn)
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "are you there")
    engine = DedupingEngine()
    engine.recorded.add(input_key(chat_id))
    rt = _with_stall_seconds(harness_runtime(engine, agent_id=agent_id), 0.5)

    async with asyncio.timeout(10):
        with pytest.raises(HarnessTurnHaltedError) as halted:
            await harness_turn_node(AgentState(), rt, config(agent_id))

    assert halted.value.reason == HALT_REASON_TURN_STALLED
    assert halted.value.requeued_inbound_ids == (chat_id,)
    assert "turn stalled" in engine.retired
    _assert_requeued_pending(db_conn, (chat_id,), expected_retries=1)
    assert _halt(db_conn, agent_id) == (1, HALT_REASON_TURN_STALLED, None)
    (warning,) = loguru_records.extras(event="harness_turn_stalled", level="WARNING")
    assert warning["requeued_count"] == 1
    assert warning["stall_seconds"] == 0.5


# meiva:note
# The threshold bounds silence, not the turn: a turn that keeps streaming runs past it and completes.
async def test_a_turn_that_keeps_sending_events_outlives_the_threshold(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "think for a while")

    class SlowEngine(FakeEngine):
        streaming: asyncio.Task[None] | None = None

        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            self.delivered.append(list(inputs))
            keys = tuple(item.key for item in inputs)

            async def stream() -> None:
                self.emit(
                    TurnStarted("t1", TurnOrigin.INPUT, keys, 1),
                    *(InputInContext(key, "t1", 1) for key in keys),
                )
                for _ in range(6):
                    await asyncio.sleep(0.2)
                    self.emit(TextDelta("t1", "."))
                self.emit(completed("t1", "MEIVA_HIDE_FROM_USER\ndone"))

            self.streaming = asyncio.get_running_loop().create_task(stream())

    engine = SlowEngine()
    rt = _with_stall_seconds(harness_runtime(engine, agent_id=agent_id), 0.5)

    async with asyncio.timeout(10):
        _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, chat_id) == "done"
    assert engine.retired == []


# meiva:note
# Regression for #7676 (task #7520): a turn that already delivered an answer before going silent
# still raises HarnessTurnHaltedError (marks_crash=False) without requeuing the answered row, and
# the delivered AssistantText has already refreshed last_active_at / last_message_text and cleared
# any prior fatal marker so corpse_reaper cannot terminate the live agent.
async def test_a_turn_stalling_after_delivered_output_halts_without_requeue_or_crash_mark(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
    loguru_records: LogRecords,
) -> None:
    agent_id = agent_row(db_conn)
    db_conn.execute(
        "UPDATE agents_meta SET last_turn_fatal_at = now() - interval '20 minutes', "
        "last_turn_fatal_reason = 'prior error' WHERE id = %s",
        (agent_id,),
    )
    db_conn.commit()
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "activation ratio question")

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = tuple(item.key for item in inputs)
        return [
            TurnStarted("t1", TurnOrigin.INPUT, keys, 1),
            *(InputInContext(key, "t1", 1) for key in keys),
            AssistantText("t1", "MEIVA_DISPLAY_TO_USER\nKimi K2 is ~3.2% active."),
        ]

    engine = FakeEngine(script=script)
    rt = _with_stall_seconds(harness_runtime(engine, agent_id=agent_id), 0.3)

    async with asyncio.timeout(10):
        with pytest.raises(HarnessTurnHaltedError) as halted:
            await harness_turn_node(AgentState(), rt, config(agent_id))

    assert halted.value.reason == HALT_REASON_TURN_STALLED
    assert halted.value.requeued_inbound_ids == ()
    assert row_status(db_conn, chat_id) == "done"
    assert _halt(db_conn, agent_id) == (0, None, None)
    assert "turn stalled" in engine.retired
    db_conn.commit()
    row = db_conn.execute(
        "SELECT last_message_text, last_turn_fatal_at, last_turn_fatal_reason "
        "FROM agents_meta WHERE id = %s",
        (agent_id,),
    ).fetchone()
    assert row == ("Kimi K2 is ~3.2% active.", None, None)
    (warning,) = loguru_records.extras(event="harness_turn_stalled", level="WARNING")
    assert warning["requeued_count"] == 0


# meiva:note
# Regression for #7676 (task #7520): Antigravity's schedule(DurationSeconds=N) timer blocks inside
# the outer turn without emitting events. Extending the silence floor to the declared timer due
# time lets the turn wait cleanly past stall_seconds and complete when the timer fires.
async def test_a_turn_waiting_on_an_explicit_schedule_timer_outlives_the_stall_threshold(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    chat_id = insert_row(db_conn, agent_id, "chat", "user", "check training in a bit")

    class TimerEngine(FakeEngine):
        waiting: asyncio.Task[None] | None = None

        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            self.delivered.append(list(inputs))
            keys = tuple(item.key for item in inputs)

            async def run_timer() -> None:
                self.emit(
                    TurnStarted("t1", TurnOrigin.INPUT, keys, 1),
                    *(InputInContext(key, "t1", 1) for key in keys),
                    ToolUse(
                        "t1",
                        "t1#1",
                        "schedule",
                        {"DurationSeconds": "1", "Prompt": "check metrics"},
                    ),
                    ToolResult("t1", "t1#1", False, "Timer: 1s"),
                    AssistantText("t1", "MEIVA_DISPLAY_TO_USER\nscheduled check"),
                )
                await asyncio.sleep(0.7)
                self.emit(completed("t1", "MEIVA_DISPLAY_TO_USER\ntimer fired"))

            self.waiting = asyncio.get_running_loop().create_task(run_timer())

    engine = TimerEngine()
    rt = _with_stall_seconds(harness_runtime(engine, agent_id=agent_id), 0.3)

    async with asyncio.timeout(10):
        _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert row_status(db_conn, chat_id) == "done"
    assert engine.retired == []


async def test_a_failed_schedule_timer_does_not_extend_the_stall_threshold(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    _ = insert_row(db_conn, agent_id, "chat", "user", "schedule a timer")

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        keys = tuple(item.key for item in inputs)
        return [
            TurnStarted("t1", TurnOrigin.INPUT, keys, 1),
            *(InputInContext(key, "t1", 1) for key in keys),
            ToolUse("t1", "t1#1", "schedule", {"DurationSeconds": 60, "Prompt": "check"}),
            ToolResult("t1", "t1#1", True, "invalid timer"),
        ]

    engine = FakeEngine(script=script)
    rt = _with_stall_seconds(harness_runtime(engine, agent_id=agent_id), 0.3)

    async with asyncio.timeout(5):
        with pytest.raises(HarnessTurnHaltedError) as halted:
            await harness_turn_node(AgentState(), rt, config(agent_id))

    assert halted.value.reason == HALT_REASON_TURN_STALLED
    assert "turn stalled" in engine.retired
