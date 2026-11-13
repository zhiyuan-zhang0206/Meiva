# meiva:note
# One wake's rows and the pre-turn hook note reach an external harness as one input, so the model's
# first request holds all of them and a passive note never opens a turn the person's message misses.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    in_a_turn,
    input_key,
    insert_row,
    rejecting,
    row_status,
    timeline_inbound_ids,
    wait_until,
)
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.graph.llm_errors import FatalLLMStreamError
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import MeivaContext
from base.config.tests.configuration import defaults
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    BridgeEvent,
    HarnessInput,
    HarnessNotDeliveredError,
    Reconciliation,
    TextBlock,
)
from base.native_process.turn_identity import HostedTurnResources


def _without_timestamps(
    engine: FakeEngine,
    aops_pool: AsyncConnectionPool,
    resources: HostedTurnResources,
    *,
    agent_id: int,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> Runtime[MeivaContext]:
    context = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    ).context
    slices = AgentSlices.resolve(default_reader=defaults(general__message_timestamps=False))
    return Runtime(context=replace(context, agent=slices))


_PASSIVE = '{"note_tag": "lifecycle_interrupt", "passive": true}'
_HOOK_NOTE = "Skill recommendation: read the `inspect-a-trace` skill."


async def test_a_passive_note_a_message_and_a_hook_note_open_one_turn_that_holds_all_three(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    passive = insert_row(
        db_conn, agent_id, "system_note", "user", "Your turn was interrupted.", _PASSIVE
    )
    chat = insert_row(db_conn, agent_id, "chat", "user", "carry on please")
    engine = FakeEngine()
    note: dict[str, object] = {
        "key": f"hook-notes-{chat}",
        "prompt": _HOOK_NOTE,
        "timeline_items": [],
    }
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    _ = await harness_turn_node(AgentState(harness_inputs=[note]), rt, config(agent_id))

    [[one]] = engine.delivered
    assert one.key == input_key(chat), "named by the first row that is not passive"
    assert (
        one.text.index("was interrupted") < one.text.index("carry on") < one.text.index(_HOOK_NOTE)
    )
    assert [row_status(db_conn, i) for i in (passive, chat)] == ["done", "done"]
    assert await timeline_inbound_ids(aops_pool, agent_id) == [chat], "each row keeps its entry"


def _claimed_group(db: psycopg.Connection, agent_id: int, key: str) -> list[int]:
    ids: list[int] = []
    for text in ("first of the wake", "second of the wake"):
        row = db.execute(
            "INSERT INTO inbound_messages (agent_id, content, kind, source, status, claimed_at, "
            "payload) VALUES (%s, %s, 'chat', 'user', 'claimed', now(), "
            "jsonb_build_object('harness_input_key', %s::text)) RETURNING id",
            (agent_id, text, key),
        ).fetchone()
        assert row is not None
        ids.append(row[0])
    db.commit()
    return ids


async def test_a_crash_settles_a_wakes_rows_by_the_one_key_they_were_handed_over_under(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    seen = _claimed_group(db_conn, agent_id, "inbound-1000")
    lost = _claimed_group(db_conn, agent_id, "inbound-2000")
    engine = FakeEngine()
    engine.records = {"inbound-1000": Reconciliation.IN_CONTEXT}
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert [row_status(db_conn, i) for i in seen] == ["done", "done"], "one answer, both rows"
    assert sorted(engine.reconciled[0]) == ["inbound-1000", "inbound-2000"], "asked per input"
    redelivered = [i for batch in engine.delivered for i in batch if "second" in i.text]
    assert [i.key for i in redelivered] == [input_key(lost[0])], "the other group, again, as one"
    assert "first of the wake" in redelivered[0].text


def _payload_keys(db: psycopg.Connection, rows: Sequence[int]) -> list[object]:
    db.commit()
    found = db.execute(
        "SELECT payload->'harness_input_key' FROM inbound_messages WHERE id=ANY(%s) ORDER BY id",
        (list(rows),),
    ).fetchall()
    return [row[0] for row in found]


def _hint_and_note(chat: int) -> AgentState:
    return AgentState(
        harness_inputs=[
            {"key": f"hook-notes-{chat}", "prompt": _HOOK_NOTE, "timeline_items": []},
            {"key": "reconciled-resume-1", "prompt": "You were restarted.", "timeline_items": []},
        ]
    )


async def test_the_rows_are_marked_before_the_harness_is_handed_the_input_and_a_refusal_clears_it(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    rows = [insert_row(db_conn, agent_id, "chat", "user", text) for text in ("one", "two")]
    seen: list[list[object]] = []

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        seen.append(_payload_keys(db_conn, rows))
        return (
            rejecting()(inputs)
            if len(seen) == 1
            else in_a_turn([i.key for i in inputs], "MEIVA_DISPLAY_TO_USER\nok")
        )

    engine = FakeEngine(script=script)
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    async with asyncio.timeout(20), asyncio.TaskGroup() as tasks:
        node = tasks.create_task(harness_turn_node(AgentState(), rt, config(agent_id)))
        await wait_until(lambda: len(seen) == 1, "the first delivery")
        await wait_until(
            lambda: [row_status(db_conn, i) for i in rows] == ["pending"] * 2, "the requeue"
        )
        assert _payload_keys(db_conn, rows) == [None, None], "a requeue clears the mark"
        await node

    assert seen[0] == [input_key(rows[0])] * 2, "marked before the harness had the input"
    assert [i.key for batch in engine.delivered for i in batch] == [input_key(rows[0])] * 2


async def test_a_refused_wake_is_handed_over_again_with_its_hint_and_hook_note(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    chat = insert_row(db_conn, agent_id, "chat", "user", "carry on")
    attempts = 0

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        nonlocal attempts
        attempts += 1
        return (
            rejecting()(inputs)
            if attempts == 1
            else in_a_turn([i.key for i in inputs], "MEIVA_DISPLAY_TO_USER\nok")
        )

    engine = FakeEngine(script=script)
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    async with asyncio.timeout(20):
        _ = await harness_turn_node(_hint_and_note(chat), rt, config(agent_id))

    first, second = [batch[0].text for batch in engine.delivered]
    assert first == second, "the same one input, hint and hook note included"
    assert first.index("restarted") < first.index("carry on") < first.index(_HOOK_NOTE)


async def test_a_wake_the_session_could_not_take_is_delivered_with_its_hint_and_hook_note_later(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    chat = insert_row(db_conn, agent_id, "chat", "user", "carry on")
    state = _hint_and_note(chat)

    class Unstartable(FakeEngine):
        async def deliver(
            self, inputs: Sequence[HarnessInput], *, compaction_note: str = ""
        ) -> None:
            raise HarnessNotDeliveredError("session could not start")

    with pytest.raises(FatalLLMStreamError):
        _ = await harness_turn_node(
            state,
            runtime(
                Unstartable(),
                ops_pool=aops_pool,
                hosted_resources=hosted_resources,
                agent_id=agent_id,
                database_gate=database_gate,
                event_bus=event_bus,
            ),
            config(agent_id),
        )
    engine = FakeEngine()
    _ = await harness_turn_node(
        state,
        runtime(
            engine,
            ops_pool=aops_pool,
            hosted_resources=hosted_resources,
            agent_id=agent_id,
            database_gate=database_gate,
            event_bus=event_bus,
        ),
        config(agent_id),
    )

    [[one]] = engine.delivered
    assert one.key == input_key(chat)
    assert one.text.index("restarted") < one.text.index("carry on") < one.text.index(_HOOK_NOTE)


async def test_a_wakes_rows_reconcile_as_one_input_the_harness_stored_or_cannot_tell_about(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    stored = _claimed_group(db_conn, agent_id, "inbound-3000")
    unknown = _claimed_group(db_conn, agent_id, "inbound-4000")
    engine = FakeEngine()
    engine.records = {
        "inbound-3000": Reconciliation.PERSISTED_UNDELIVERED,
        "inbound-4000": Reconciliation.UNKNOWN,
    }
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert engine.adopted == ["inbound-3000"], "the group is adopted once, under its one key"
    assert [row_status(db_conn, i) for i in stored] == ["claimed", "claimed"]
    assert [row_status(db_conn, i) for i in unknown] == ["done", "done"], "both marked, none sent"


def _hints_only() -> AgentState:
    return AgentState(
        harness_inputs=[
            {"key": "hook-notes-9", "prompt": _HOOK_NOTE, "timeline_items": []},
            {"key": "reconciled-resume-1", "prompt": "You were restarted.", "timeline_items": []},
            {"key": "context-reset-2", "prompt": "Context was reset.", "timeline_items": []},
        ]
    )


async def test_a_wake_with_no_row_text_hands_its_synthetic_inputs_over_as_one_input(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    _ = insert_row(db_conn, agent_id, "system_note", "user", "  ", _PASSIVE)
    engine = FakeEngine()
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    _ = await harness_turn_node(_hints_only(), rt, config(agent_id))

    [[one]] = engine.delivered
    assert one.key == "reconciled-resume-1", "named by the first hint; the hook note comes last"
    assert (
        one.text.index("restarted")
        < one.text.index("Context was reset")
        < one.text.index(_HOOK_NOTE)
    )


async def test_a_refused_synthetic_only_wake_is_handed_over_again_as_the_same_one_input(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    attempts = 0

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        nonlocal attempts
        attempts += 1
        return (
            rejecting()(inputs)
            if attempts == 1
            else in_a_turn([i.key for i in inputs], "MEIVA_DISPLAY_TO_USER\nok")
        )

    engine = FakeEngine(script=script)
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    async with asyncio.timeout(20):
        _ = await harness_turn_node(_hints_only(), rt, config(agent_id))

    first, second = engine.delivered
    assert len(first) == len(second) == 1, "one input each time, never one per synthetic input"
    assert first[0].key == second[0].key and first[0].text == second[0].text
    assert second[0].text.count(_HOOK_NOTE) == 1, "nothing rides twice"


def test_plan_wake_merges_synthetic_inputs_and_keeps_a_single_one_as_it_was() -> None:
    from agent.graph.claim.harness_foreground import Handed, plan_wake

    one = (Handed("control-1", None, ()), "restarted")
    two = (Handed("hook-notes-1", None, ()), "note")

    single = plan_wake([one], [], {}, hook_notes_key="hook-notes-")
    merged = plan_wake([two, one], [], {}, hook_notes_key="hook-notes-")

    assert [i.text for i in single.inputs] == ["restarted"] and single.handed == (one[0],)
    [wake] = merged.handed
    assert wake.key == "control-1" and wake.row_ids == () and merged.group is None
    assert [i.text for i in merged.inputs] == ["restarted\n\nnote"]
    assert merged.folded == () and merged.prompts["control-1"] == "restarted\n\nnote"


def test_plan_wake_bounds_the_images_of_the_merged_input_not_of_each_row() -> None:
    import base64

    from agent.graph.claim.harness_foreground import Handed, plan_wake
    from agent.graph.claim.harness_render import MAX_IMAGE_BYTES
    from base.lm.harness import ImageBlock

    data = base64.b64encode(b"x" * MAX_IMAGE_BYTES).decode("ascii")
    block = ImageBlock(mime_type="image/png", data_base64=data)
    rows = [(Handed(f"inbound-{i}", i, ()), f"message {i}", False) for i in range(1, 5)]
    images = {f"inbound-{i}": (block,) for i in range(1, 5)}

    [wake] = plan_wake([], rows, images, hook_notes_key="hook-notes-").inputs

    assert len(wake.images) == 3, "four 5 MB images in one input: the fourth is left out"
    assert "1 attached image was not sent inline" in wake.text


def test_plan_wake_makes_a_block_per_message_with_its_images_behind_it() -> None:
    from agent.graph.claim.harness_foreground import Handed, plan_wake
    from base.lm.harness import ImageBlock, TextBlock

    image = ImageBlock(mime_type="image/png", data_base64="aGk=")
    rows = [
        (Handed("inbound-1", 1, ()), "first message", False),
        (Handed("inbound-2", 2, ()), "second message", False),
    ]
    synthetic = [
        (Handed("hook-notes-2", None, ()), "a note"),
        (Handed("control-1", None, ()), "a hint"),
    ]

    plan = plan_wake(
        synthetic,
        rows,
        {"inbound-1": (image,), "inbound-2": (image, image)},
        hook_notes_key="hook-notes-",
    )

    [wake] = plan.inputs
    assert wake.blocks == (
        TextBlock("a hint"),
        TextBlock("first message"),
        image,
        TextBlock("second message"),
        image,
        image,
        TextBlock("a note"),
    )
    assert plan.group is not None and plan.group.row_ids == (1, 2), "rows are marked for a crash"


async def test_each_message_of_a_wake_keeps_its_own_timeline_entry(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    rows = [insert_row(db_conn, agent_id, "chat", "user", t) for t in ("first", "second")]
    engine = FakeEngine()
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    [[one]] = engine.delivered
    texts = [b.text for b in one.blocks if isinstance(b, TextBlock)]
    assert len(texts) == 2 and texts[0].endswith("first") and texts[1].endswith("second")
    assert await timeline_inbound_ids(aops_pool, agent_id) == rows, "a timeline entry per message"


async def test_passive_rows_are_not_handed_over_when_the_only_row_that_is_not_passive_has_no_text(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    blank = insert_row(db_conn, agent_id, "chat", "user", "  ")
    passive = insert_row(
        db_conn, agent_id, "system_note", "user", "Your turn was interrupted.", _PASSIVE
    )
    attempts = 0

    def script(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        nonlocal attempts
        attempts += 1
        return (
            rejecting()(inputs)
            if attempts == 1
            else in_a_turn([i.key for i in inputs], "MEIVA_DISPLAY_TO_USER\nok")
        )

    engine = FakeEngine(script=script)
    rt = _without_timestamps(
        engine,
        aops_pool,
        hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    async with asyncio.timeout(20):
        _ = await harness_turn_node(_hints_only(), rt, config(agent_id))

    first, second = engine.delivered
    assert first[0].text == second[0].text, "the refused input is handed over again, once"
    assert second[0].text.count(_HOOK_NOTE) == 1 and second[0].text.count("restarted") == 1
    assert "interrupted" not in first[0].text, "a passive note is never handed over on its own"
    assert row_status(db_conn, blank) == "done"
    assert row_status(db_conn, passive) == "pending", (
        "it waits for the next input that is not passive"
    )


def test_the_note_about_left_out_images_follows_the_last_message_before_the_hook_note() -> None:
    import base64

    from agent.graph.claim.harness_foreground import Handed, plan_wake
    from agent.graph.claim.harness_render import MAX_IMAGE_BYTES, skipped_images_note
    from base.lm.harness import ImageBlock, TextBlock

    data = base64.b64encode(b"x" * MAX_IMAGE_BYTES).decode("ascii")
    image = ImageBlock(mime_type="image/png", data_base64=data)
    rows = [(Handed(f"inbound-{i}", i, ()), f"message {i}", False) for i in range(1, 5)]
    images = {f"inbound-{i}": (image,) for i in range(1, 5)}
    note = (Handed("hook-notes-4", None, ()), "a hook note")

    [wake] = plan_wake([note], rows, images, hook_notes_key="hook-notes-").inputs

    assert wake.blocks == (
        TextBlock("message 1"),
        image,
        TextBlock("message 2"),
        image,
        TextBlock("message 3"),
        image,
        TextBlock("message 4"),
        TextBlock(skipped_images_note(1)),
        TextBlock("a hook note"),
    )


def test_passive_rows_alone_are_held_and_never_become_the_wake() -> None:
    from agent.graph.claim.harness_foreground import Handed, plan_wake

    passive = (Handed("inbound-2", 2, ()), "interrupted", True)
    blank = (Handed("inbound-1", 1, ()), "  ", False)
    hint = (Handed("control-1", None, ()), "restarted")

    with_hint = plan_wake([hint], [blank, passive], {}, hook_notes_key="hook-notes-")
    alone = plan_wake([], [blank, passive], {}, hook_notes_key="hook-notes-")

    assert [i.text for i in with_hint.inputs] == ["restarted"]
    assert with_hint.handed[0].row_ids == () and with_hint.group is None
    assert with_hint.held == (passive[0],) and with_hint.settled_now == (blank[0],)
    assert alone.inputs == () and alone.held == (passive[0],)


async def test_a_synthetic_input_with_no_text_fails_before_any_row_is_reserved(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    agent_id = agent_row(db_conn)
    chat = insert_row(db_conn, agent_id, "chat", "user", "carry on")
    state = AgentState(
        harness_inputs=[{"key": "hook-notes-1", "prompt": "  \n", "timeline_items": []}]
    )
    engine = FakeEngine()
    rt = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        agent_id=agent_id,
        database_gate=database_gate,
        event_bus=event_bus,
    )

    with pytest.raises(ValueError, match="hook-notes-1"):
        _ = await harness_turn_node(state, rt, config(agent_id))

    assert engine.delivered == [] and row_status(db_conn, chat) == "pending"


async def test_a_crash_rebuilds_the_blocks_of_a_wake_as_the_first_delivery_made_them(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
) -> None:
    fresh_agent = agent_row(db_conn)
    for text in ("first of the wake", "second of the wake"):
        _ = insert_row(db_conn, fresh_agent, "chat", "user", text)
    fresh = FakeEngine()
    _ = await harness_turn_node(
        AgentState(),
        _without_timestamps(
            fresh,
            aops_pool,
            hosted_resources,
            agent_id=fresh_agent,
            database_gate=database_gate,
            event_bus=event_bus,
        ),
        config(fresh_agent),
    )
    crashed_agent = agent_row(db_conn)
    _ = _claimed_group(db_conn, crashed_agent, "inbound-6000")
    _ = _claimed_group(db_conn, crashed_agent, "inbound-7000")
    rebuilt = FakeEngine()
    rebuilt.records = {"inbound-6000": Reconciliation.IN_CONTEXT}

    _ = await harness_turn_node(
        AgentState(),
        _without_timestamps(
            rebuilt,
            aops_pool,
            hosted_resources,
            agent_id=crashed_agent,
            database_gate=database_gate,
            event_bus=event_bus,
        ),
        config(crashed_agent),
    )

    [[first]] = fresh.delivered
    [[again]] = rebuilt.delivered
    assert [b.text for b in first.blocks if isinstance(b, TextBlock)] == [
        "first of the wake",
        "second of the wake",
    ]
    assert again.blocks[1:] == first.blocks, "the same message blocks, delivered again as one input"
    assert isinstance(again.blocks[0], TextBlock) and "restarted" in again.blocks[0].text
