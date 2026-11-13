# meiva:note
# A Codex turn through `harness_turn_node` with the rollout as the one source of its timeline: the fake CLI writes the
# rows Codex writes for a turn (its own `clientUserMessageId` on the input, one `AgentMessage` per reply), and what the
# page, the user and the inbox get comes from reading them. Nothing is written to the stored timeline; a reply is
# delivered once under the CLI's own message id however often it is read; the first sink after the switch from the
# old keys does not send the old replies again; a turn with no readable record fails.
# meiva:link docs/decisions/agents/graph/2026-11-13-timeline-read-from-the-record.md

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    config,
    in_a_turn,
    insert_row,
    row_status,
    runtime,
)
from agent.graph.llm_errors import FatalLLMStreamError
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import HostedTurnResources, MeivaContext
from base.agents.history.harness_messages import aload_harness_timeline_items
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.events.live.tests.live_capture import live_events as live_events
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import BridgeEvent, HarnessInput
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources

SESSION = "01a1d14b-0f88-7ee3-8636-d3b8c1507a76"
REPLY = "Done: the command printed hello."
DAY = "sessions/2026/11/13"
ROLLOUT = f"rollout-2026-11-13T03-45-19-{SESSION}.jsonl"


# meiva:note
# The CLI as far as the record goes: the input it takes is written as the rows Codex writes for a turn.
class RecordingCodex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.turns = 0

    def _row(self, kind: str, payload: dict[str, Any]) -> str:
        stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return json.dumps({"timestamp": stamp, "ordinal": 0, "type": kind, "payload": payload})

    def write(self, key: str) -> None:
        self.turns += 1
        n, turn = self.turns, f"turn-{self.turns}"
        ms = int(datetime.now(UTC).timestamp() * 1000)
        times = {"started_at_ms": ms, "completed_at_ms": ms}

        def item(payload: dict[str, Any]) -> str:
            return self._row(
                "event_msg",
                {"type": "item_completed", "turn_id": turn, "item": payload, **times},
            )

        rows = [
            self._row("event_msg", {"type": "task_started", "turn_id": turn}),
            item(
                {
                    "type": "UserMessage",
                    "id": f"u-{n}",
                    "client_id": key,
                    "content": [{"type": "text", "text": "hello", "text_elements": []}],
                }
            ),
            item(
                {
                    "type": "AgentMessage",
                    "id": f"msg_{n}",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": REPLY}],
                }
            ),
            self._row("event_msg", {"type": "task_complete", "turn_id": turn}),
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            handle.write("\n".join(rows) + "\n")

    def __call__(self, inputs: Sequence[HarnessInput]):
        keys = [item.key for item in inputs]
        self.write(keys[0])
        return in_a_turn(keys, REPLY)


def _codex_agent(db_conn: psycopg.Connection) -> int:
    agent_id = agent_row(db_conn)
    db_conn.execute("UPDATE agents_meta SET harness='codex' WHERE id=%s", (agent_id,))
    db_conn.commit()
    return agent_id


def _engine(script: Any = None) -> FakeEngine:
    engine = FakeEngine(script=script or (lambda _inputs: []))
    engine.session_id = SESSION
    return engine


def _wired(
    tmp_path: Path,
    engine: FakeEngine,
    pool: AsyncConnectionPool,
    resources: HostedTurnResources,
    gate: ProcessDbGate,
    live: LiveEvents,
) -> Runtime[MeivaContext]:
    base = runtime(engine, ops_pool=pool, hosted_resources=resources, publisher=live.publisher)
    return Runtime(
        context=replace(
            base.context,
            harness=Harness.CODEX,
            meiva_home=tmp_path / "home",
            environ={"HOME": str(tmp_path), "CODEX_HOME": str(tmp_path / "codex")},
            db=Database.from_settings(gate=gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            agent=AgentSlices.resolve({"user_delivery_mode": "naive"}, default_reader=defaults()),
        )
    )


def _publications(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str]]:
    db_conn.commit()
    rows = db_conn.execute(
        "SELECT source_key, content FROM agent_publications WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall()
    return [(str(key), str(content)) for key, content in rows]


def _assert_page_saw_the_message(events: list[dict[str, Any]], row: int) -> None:
    snapshots = [e for e in events if e.get("role") == "timeline_snapshot"]
    shown = {item["item_id"]: item for e in snapshots for item in e["items"]}
    first = next(iter(shown.values()))
    assert (first["kind"], first["inbound_id"]) == ("inbound_chat", row)
    assert all(re.fullmatch(r"h\d+\.\d+", item_id) for item_id in shown)
    committed = [e["inbound_id"] for e in events if e.get("role") == "inbound_committed"]
    assert committed == [row]


async def test_a_codex_turn_reaches_the_page_the_user_and_the_inbox_through_the_rollout(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = _codex_agent(db_conn)
    row = insert_row(db_conn, agent_id, "chat", "user", "hello")
    cli = RecordingCodex(tmp_path / "codex" / DAY / ROLLOUT)
    rt = _wired(tmp_path, _engine(cli), aops_pool, hosted_resources, database_gate, live_events)

    command = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert command.goto == "claim"
    assert row_status(db_conn, row) == "done", "the input is settled without a timeline write"
    async with aops_pool.connection() as conn:
        assert await aload_harness_timeline_items(conn, agent_id) == [], "no stored rows"
    _assert_page_saw_the_message(
        [json.loads(payload) for payload in await live_events.payloads()], row
    )
    published = _publications(db_conn, agent_id)
    assert len(published) == 1 and published[0][1] == REPLY
    assert published[0][0] == "harness:codex:msg_1"
    again = _wired(tmp_path, _engine(), aops_pool, hosted_resources, database_gate, live_events)
    _ = await harness_turn_node(AgentState(), again, config(agent_id))
    assert _publications(db_conn, agent_id) == published, "a restart delivers nothing twice"


async def test_the_switch_from_the_old_keys_does_not_resend_and_what_follows_is_delivered_by_key(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = _codex_agent(db_conn)
    cli = RecordingCodex(tmp_path / "codex" / DAY / ROLLOUT)
    cli.write("inbound-100")
    db_conn.execute(
        "INSERT INTO agent_publications (agent_id, source_key, mode, content, suggestions) "
        "VALUES (%s, 'harness:h5.0', 'naive', %s, '[]'::jsonb)",
        (agent_id, REPLY),
    )
    db_conn.commit()
    old = _publications(db_conn, agent_id)

    def run(engine: FakeEngine):
        rt = _wired(tmp_path, engine, aops_pool, hosted_resources, database_gate, live_events)
        return harness_turn_node(AgentState(), rt, config(agent_id))

    _ = await run(_engine())
    assert _publications(db_conn, agent_id) == old, "the reply the old keys delivered is not resent"
    _ = insert_row(db_conn, agent_id, "chat", "user", "again")
    _ = await run(_engine(cli))
    new = _publications(db_conn, agent_id)
    assert [key for key, _content in new][1:] == ["harness:codex:msg_2"]
    cli.write("inbound-200")
    _ = await run(_engine())
    _ = await run(_engine())
    assert [key for key, _content in _publications(db_conn, agent_id)][1:] == [
        "harness:codex:msg_2",
        "harness:codex:msg_3",
    ], "the later reply arrives, once"


async def test_a_turn_with_no_rollout_to_show_for_it_fails_and_names_where_it_looked(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = _codex_agent(db_conn)
    _ = insert_row(db_conn, agent_id, "chat", "user", "hello")

    def unseen(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        return in_a_turn([item.key for item in inputs], "a reply nobody can see")

    rt = _wired(tmp_path, _engine(unseen), aops_pool, hosted_resources, database_gate, live_events)

    with pytest.raises(FatalLLMStreamError) as failed:
        _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert SESSION in str(failed.value) and str(tmp_path / "codex" / "sessions") in str(
        failed.value
    )


async def test_a_rollout_in_a_form_the_reader_does_not_read_fails_the_turn_and_is_reported(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
    loguru_records: LogRecords,
) -> None:
    agent_id = _codex_agent(db_conn)
    _ = insert_row(db_conn, agent_id, "chat", "user", "hello")
    packed = tmp_path / "codex" / DAY / f"{ROLLOUT}.zst"
    packed.parent.mkdir(parents=True)
    packed.write_text("x")

    def unseen(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        return in_a_turn([item.key for item in inputs], "a reply nobody can see")

    rt = _wired(tmp_path, _engine(unseen), aops_pool, hosted_resources, database_gate, live_events)

    with pytest.raises(FatalLLMStreamError):
        _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    (event,) = loguru_records.events("harness_record_unreadable")
    assert event["extra"]["name"] == packed.name and event["extra"]["session_id"] == SESSION


class RecordingAntigravity:
    def __init__(self, path: Path) -> None:
        self.path = path

    def write(self) -> None:
        stamp = datetime.now(UTC).isoformat()
        rows: list[dict[str, object]] = [
            {"type": "USER_INPUT", "content": "<USER_REQUEST>hello</USER_REQUEST>"},
            {"type": "PLANNER_RESPONSE", "content": REPLY, "tool_calls": []},
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as handle:
            for row in rows:
                handle.write(
                    json.dumps({"step_index": 1, "status": "DONE", "created_at": stamp, **row})
                    + "\n"
                )


async def test_antigravity_content_matching_and_conversation_publications_are_idempotent(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = agent_row(db_conn)
    db_conn.execute("UPDATE agents_meta SET harness='antigravity' WHERE id=%s", (agent_id,))
    db_conn.commit()
    row_id = insert_row(db_conn, agent_id, "chat", "user", "hello")
    path = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / SESSION
        / ".system_generated/logs/transcript_full.jsonl"
    )
    cli = RecordingAntigravity(path)

    def record(inputs: Sequence[HarnessInput]):
        cli.write()
        return in_a_turn([item.key for item in inputs], REPLY)

    def rt(engine: FakeEngine) -> Runtime[MeivaContext]:
        wired = _wired(tmp_path, engine, aops_pool, hosted_resources, database_gate, live_events)
        return Runtime(context=replace(wired.context, harness=Harness.ANTIGRAVITY))

    await harness_turn_node(AgentState(), rt(_engine(record)), config(agent_id))
    assert row_status(db_conn, row_id) == "done"
    first = _publications(db_conn, agent_id)
    assert len(first) == 1 and first[0][0].startswith(f"harness:antigravity:{SESSION}:")
    _assert_page_saw_the_message([json.loads(p) for p in await live_events.payloads()], row_id)
    await harness_turn_node(AgentState(), rt(_engine()), config(agent_id))
    assert _publications(db_conn, agent_id) == first
    async with aops_pool.connection() as conn:
        assert await aload_harness_timeline_items(conn, agent_id) == []


@pytest.mark.parametrize("harness", [Harness.CODEX, Harness.ANTIGRAVITY])
async def test_finish_waits_for_a_record_written_after_the_cli_completed_signal(
    harness: Harness,
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = _codex_agent(db_conn)
    _ = db_conn.execute("UPDATE agents_meta SET harness=%s WHERE id=%s", (harness.value, agent_id))
    db_conn.commit()
    _ = insert_row(db_conn, agent_id, "chat", "user", "hello")
    if harness is Harness.ANTIGRAVITY:
        cli = RecordingAntigravity(
            tmp_path
            / ".gemini/antigravity-cli/brain"
            / SESSION
            / ".system_generated/logs/transcript_full.jsonl"
        )

        def write(key: str) -> None:
            del key
            cli.write()
    else:
        codex = RecordingCodex(tmp_path / "codex" / DAY / ROLLOUT)
        write = codex.write

    def delayed(inputs: Sequence[HarnessInput]):
        asyncio.get_running_loop().call_later(0.05, write, inputs[0].key)
        return in_a_turn([item.key for item in inputs], REPLY)

    rt = _wired(tmp_path, _engine(delayed), aops_pool, hosted_resources, database_gate, live_events)
    rt = Runtime(context=replace(rt.context, harness=harness))
    await harness_turn_node(AgentState(), rt, config(agent_id))
    rows = _publications(db_conn, agent_id)
    assert len(rows) == 1 and rows[0][1] == REPLY
    if harness is Harness.CODEX:
        assert rows == [("harness:codex:msg_1", REPLY)]
    else:
        assert rows[0][0].startswith(f"harness:antigravity:{SESSION}:")
