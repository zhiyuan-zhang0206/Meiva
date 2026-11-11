# meiva:note
# One wake's rows and the pre-turn hook note reach an external harness as one input, so the model's
# first request holds all of them and a passive note never opens a turn the person's message misses.
# meiva:link agent/graph/docs/harness-turn.md

from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    input_key,
    insert_row,
    row_status,
    timeline_inbound_ids,
)
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.claim.tests.test_harness_turn import hosted_resources as hosted_resources
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.lm.harness import Reconciliation
from base.native_process.turn_identity import HostedTurnResources

_PASSIVE = '{"note_tag": "lifecycle_interrupt", "passive": true}'
_HOOK_NOTE = "Skill recommendation: read the `inspect-a-trace` skill."


async def test_a_passive_note_a_message_and_a_hook_note_open_one_turn_that_holds_all_three(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    hosted_resources: HostedTurnResources,
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
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

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
) -> None:
    agent_id = agent_row(db_conn)
    seen = _claimed_group(db_conn, agent_id, "inbound-1000")
    lost = _claimed_group(db_conn, agent_id, "inbound-2000")
    engine = FakeEngine()
    engine.records = {"inbound-1000": Reconciliation.IN_CONTEXT}
    rt = runtime(engine, ops_pool=aops_pool, hosted_resources=hosted_resources)

    _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert [row_status(db_conn, i) for i in seen] == ["done", "done"], "one answer, both rows"
    assert sorted(engine.reconciled[0]) == ["inbound-1000", "inbound-2000"], "asked per input"
    redelivered = [i for batch in engine.delivered for i in batch if "second" in i.text]
    assert [i.key for i in redelivered] == [input_key(lost[0])], "the other group, again, as one"
    assert "first of the wake" in redelivered[0].text
