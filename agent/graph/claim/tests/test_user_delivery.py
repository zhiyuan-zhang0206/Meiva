import json
import re
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_activity import (
    SUBAGENT_CUT,
    SUBAGENT_ITEM_CHARS,
    HarnessTurnProjector,
)
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    in_a_turn,
    insert_row,
    read_recorded_items,
    row_status,
)
from agent.graph.claim.tests.harness_support import (
    harness_runtime as harness_runtime,
)
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.hooks.user_delivery import capture_items
from agent.graph.llm_errors import FatalLLMStreamError
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import HostedTurnResources, MeivaContext
from base.agents.history.harness_messages import (
    aget_harness_call_item_id,
    aget_next_harness_message_seq,
    aload_harness_timeline_items,
    aupsert_harness_timeline_item,
)
from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.db.transaction import async_write_transaction
from base.events.live.bus import EventBus
from base.events.live.tests.live_capture import LiveEvents
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    HarnessInput,
    SubagentText,
    TextDelta,
    ToolResult,
    ToolUse,
)
from base.lm.harness.claude.delivery import key_uuid
from base.paths import workspace_dir
from meiva.tests.fixtures.pin_agent import hosted_resources as hosted_resources


async def test_sdk_harness_corrects_only_at_natural_end_and_keeps_raw_final_in_details(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    aid = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'running', 'antigravity')",
        (aid,),
    )
    db_conn.commit()
    engine = FakeEngine(reply="A private final summary")
    base = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
        agent_id=aid,
        database_gate=database_gate,
    )
    sdk = Runtime(
        context=replace(
            base.context,
            db=Database.from_settings(gate=database_gate),
            agent=AgentSlices.resolve({"user_delivery_mode": "sdk"}, default_reader=defaults()),
        )
    )
    state = AgentState(
        harness_inputs=[{"key": "human-request", "prompt": "Do the work", "timeline_items": []}],
        user_delivery_baseline=0,
    )
    with pytest.raises(FatalLLMStreamError, match="SDK delivery contract violated"):
        await harness_turn_node(state, sdk, config(aid))
    [first, second] = engine.delivered
    assert [item.key for item in first] == ["human-request"]
    assert [item.key for item in second] == ["human-request:visibility-correction"]
    async with aops_pool.connection() as conn:
        items = await read_recorded_items(conn, aid, policy=ReplyPolicy(False, "sdk"))
    raw = [item for item in items if item.kind == "agent_chat"]
    assert len(raw) == 2 and all(item.user_facing is False for item in raw)
    assert engine.interrupts == 0


async def test_harness_turn_does_not_inject_visibility_rule_and_corrects_once_on_violation(
    db_conn: psycopg.Connection,
    harness_runtime: RuntimeFixture,
) -> None:
    agent_id = agent_row(db_conn)
    engine_valid = FakeEngine(reply="MEIVA_DISPLAY_TO_USER\nhello")
    state = AgentState(
        harness_inputs=[{"key": "inbound-10", "prompt": "hello from user", "timeline_items": []}]
    )
    cmd_valid = await harness_turn_node(
        state, harness_runtime(engine_valid, agent_id=agent_id), config(agent_id)
    )
    assert cmd_valid.goto == "claim"
    assert cmd_valid.update is not None
    assert cmd_valid.update["harness_inputs"] == []
    [(prompt, _)] = engine_valid.submitted
    assert prompt == "hello from user"

    engine_invalid = FakeEngine(reply="unprefixed reply")
    cmd_invalid = await harness_turn_node(
        state, harness_runtime(engine_invalid, agent_id=agent_id), config(agent_id)
    )
    assert cmd_invalid.goto == "claim"
    [(original, _), (correction, correction_key)] = engine_invalid.submitted
    assert original == "hello from user"
    assert correction_key == "inbound-10:visibility-correction"
    assert "visibility prefix" in correction


# meiva:note
# A turn cut off mid-reply (interrupt) still leaves the text the reader was shown: it is sealed
# as it stood and published once, under the key of its timeline item, like a completed reply.
# meiva:link agent/graph/docs/harness-turn.md
@pytest.mark.parametrize("interrupted", [True, False])
async def test_harness_reply_is_published_once_whether_or_not_the_turn_was_cut_off(
    interrupted: bool,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    aid = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'running', 'antigravity')",
        (aid,),
    )
    db_conn.commit()
    deltas = (
        TextDelta(turn_id="t1", text="Half a "),
        TextDelta(turn_id="t1", text="reply"),
    )
    engine = FakeEngine(
        reply="" if interrupted else "Half a reply",
        events=(*deltas, AssistantText(turn_id="t1", text="Half a reply")),
        interrupted=interrupted,
    )
    base = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
        agent_id=aid,
        database_gate=database_gate,
    )
    naive = Runtime(
        context=replace(
            base.context,
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            agent=AgentSlices.resolve({"user_delivery_mode": "naive"}, default_reader=defaults()),
        )
    )
    state = AgentState(
        harness_inputs=[{"key": "human-request", "prompt": "Work", "timeline_items": []}]
    )
    await harness_turn_node(state, naive, config(aid))

    async with aops_pool.connection() as conn:
        items = await read_recorded_items(conn, aid, policy=ReplyPolicy(False, "naive"))
    [chat] = [item for item in items if item.kind == "agent_chat"]
    assert chat.payload == "Half a reply" and chat.activity_pending is None
    assert db_conn.execute(
        "SELECT source_key, content FROM agent_publications WHERE agent_id=%s", (aid,)
    ).fetchall() == [
        (
            f"harness:antigravity:{engine.session_id}:{chat.item_id[1:].split(chr(46))[0]}",
            "Half a reply",
        )
    ]


def _said(text: str, *, call: str = "call-agent", agent: str | None = None) -> SubagentText:
    return SubagentText(turn_id="t1", tool_use_id=call, agent=agent, text=text)


# meiva:note
# The retained adapter-event projector runs directly against a real database. Its sub-agent
# detail and old publication keys remain covered while current harness turns use their records.
# meiva:link base/lm/harness/docs/subagent-text.md
async def _subagent_turn(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
    said: tuple[BridgeEvent, ...],
    *,
    agent_id: int | None = None,
    launching: bool = True,
) -> tuple[list[TimelineItem], list[tuple[object, ...]]]:
    aid = agent_id if agent_id is not None else create_agent(db_conn)
    if agent_id is None:
        db_conn.execute(
            "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'running', 'codex')",
            (aid,),
        )
        db_conn.commit()
    launch = ToolUse(
        turn_id="t1", tool_use_id="call-agent", name="Agent", tool_input={"prompt": "go"}
    )
    launched = ToolResult(
        turn_id="t1", tool_use_id="call-agent", content="launched", is_error=False
    )
    engine = FakeEngine(
        reply="MEIVA_DISPLAY_TO_USER\nthe answer",
        events=(launch, launched, *said) if launching else said,
    )
    base = runtime(
        engine,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
    )
    naive = Runtime(
        context=replace(
            base.context,
            db=Database.from_settings(gate=database_gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            agent=AgentSlices.resolve({"user_delivery_mode": "naive"}, default_reader=defaults()),
        )
    )
    projector = await _stored_projector(naive.context, aid, aops_pool)
    for event in (
        *((launch, launched) if launching else ()),
        *said,
        AssistantText(turn_id="t1", text="MEIVA_DISPLAY_TO_USER\nthe answer"),
    ):
        await projector.on_event(event)
    await projector.finish()
    async with aops_pool.connection() as conn:
        items = await aload_harness_timeline_items(conn, aid)
    rows = db_conn.execute(
        "SELECT source_key, content FROM agent_publications WHERE agent_id=%s", (aid,)
    ).fetchall()
    return items, rows


async def _stored_projector(
    ctx: MeivaContext, agent: int, pool: AsyncConnectionPool
) -> HarnessTurnProjector:
    async with pool.connection() as conn:
        start = await aget_next_harness_message_seq(conn, agent)

    async def checkpoint(item: TimelineItem, _count: int) -> None:
        async with async_write_transaction(pool) as conn:
            saved = await aupsert_harness_timeline_item(
                conn, agent, ctx.harness, item, incarnation=ctx.original_incarnation
            )
        assert saved is not None
        await capture_items(ctx, agent, [item], source_key="harness")

    async def find_call(key: str) -> str | None:
        async with pool.connection() as conn:
            return await aget_harness_call_item_id(conn, agent, key)

    return HarnessTurnProjector(
        checkpoint=checkpoint,
        base_idx=start,
        clock=ctx.require_clock(),
        delivery_mode="naive",
        find_call=find_call,
    )


# meiva:note
# A CLI sub-agent's text is work detail: it lands on the timeline as `subagent_text` items tied to the
# spawning call, never as a reply, so it writes no delivery row (nothing unread, nothing pushed).
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_subagent_text_is_timeline_detail_and_never_a_delivery(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    said = (
        _said("ALPHA", agent="general-purpose"),
        _said("orphan", call="unseen-call"),
        _said("   "),
    )
    items, rows = await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, said
    )
    call = next(item for item in items if item.kind == "agent_code")
    detail = [
        (i.payload, i.source, i.parent_item_id, i.user_facing)
        for i in items
        if i.kind == "subagent_text"
    ]
    assert detail == [
        ("ALPHA", "general-purpose", call.item_id, False),
        ("orphan", None, None, False),
    ]
    reply = next(i for i in items if i.kind == "agent_chat")
    assert rows == [(f"harness:{reply.item_id}", "MEIVA_DISPLAY_TO_USER\nthe answer")]


# meiva:note
# Sub-agent text is cut per message; every message is still written.
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_subagent_text_is_cut_per_message_and_every_message_is_written(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    long_text = "x" * (SUBAGENT_ITEM_CHARS + 50)
    said = (_said(long_text), _said("y" * SUBAGENT_ITEM_CHARS), _said("later"))
    items, _ = await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, said
    )
    texts = [i.payload for i in items if i.kind == "subagent_text"]
    cut = texts[0]
    assert texts[1:] == ["y" * SUBAGENT_ITEM_CHARS, "later"]
    assert cut.endswith(SUBAGENT_CUT) and len(cut) < len(long_text)


def _subagent_call(call: str, name: str = "Bash", *, parent: str = "call-agent") -> ToolUse:
    return ToolUse(
        turn_id="t1",
        tool_use_id=call,
        name=name,
        tool_input={"command": "echo hi"},
        parent_tool_use_id=parent,
    )


def _subagent_result(call: str, content: str, *, parent: str = "call-agent") -> ToolResult:
    return ToolResult(
        turn_id="t1", tool_use_id=call, is_error=False, content=content, parent_tool_use_id=parent
    )


# meiva:note
# A tool call the sub-agent made lands as the usual `agent_code` / `code_output` pair, tied to the
# `agent_code` item of the call that started the sub-agent; the main agent's own calls stay untied.
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_a_subagents_tool_activity_is_tied_to_the_call_that_started_it(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    said = (
        _said("ALPHA", agent="general-purpose"),
        _subagent_call("sub-bash"),
        _subagent_result("sub-bash", "hi"),
        _subagent_call("lost-bash", parent="unknown-call"),
    )
    items, rows = await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, said
    )
    spawn = next(i for i in items if i.tool_use_id == "call-agent")
    assert spawn.parent_item_id is None
    parents = {i.tool_use_id: i.parent_item_id for i in items if i.kind == "agent_code"}
    assert parents == {"call-agent": None, "sub-bash": spawn.item_id, "lost-bash": None}, (
        "a call whose parent is nowhere stays unattributed"
    )
    outputs = {i.payload: i.parent_item_id for i in items if i.kind == "code_output"}
    assert outputs["launched"] is None and outputs["hi"] == spawn.item_id
    reply = next(i for i in items if i.kind == "agent_chat")
    assert rows == [(f"harness:{reply.item_id}", "MEIVA_DISPLAY_TO_USER\nthe answer")]


# meiva:note
# A sub-agent's tool output is cut like its text, and later calls are still written.
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_a_subagents_tool_output_is_cut(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    said = (
        _subagent_call("big"),
        _subagent_result("big", "z" * (SUBAGENT_ITEM_CHARS + 10)),
        _subagent_call("after"),
        _subagent_result("after", "late"),
    )
    items, _ = await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, said
    )
    [big] = [i for i in items if i.kind == "code_output" and i.payload.startswith("z")]
    assert big.payload.endswith(SUBAGENT_CUT) and len(big.payload) < SUBAGENT_ITEM_CHARS + 100
    assert "late" in [i.payload for i in items]
    assert not [i for i in items if i.payload == "Tool running"], "no call is left running"


# meiva:note
# A sub-agent that keeps working after the invocation that saw its spawning call ended is still tied to
# that call: the call's timeline item is found by the CLI's call id.
# meiva:link base/lm/harness/docs/subagent-text.md
async def test_activity_after_the_launching_invocation_still_finds_its_call(
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    aid = create_agent(db_conn)
    db_conn.execute(
        "INSERT INTO agents_meta (id, status, harness) VALUES (%s, 'running', 'codex')",
        (aid,),
    )
    db_conn.commit()
    await _subagent_turn(
        db_conn, aops_pool, database_gate, hosted_resources, live_events, (), agent_id=aid
    )
    items, _ = await _subagent_turn(
        db_conn,
        aops_pool,
        database_gate,
        hosted_resources,
        live_events,
        (
            _said("LATE"),
            _said("nobody's", call="never-seen"),
            _subagent_call("late-bash"),
            _subagent_result("late-bash", "hi"),
        ),
        agent_id=aid,
        launching=False,
    )
    [spawn] = [i for i in items if i.tool_use_id == "call-agent"]
    [late] = [i for i in items if i.payload == "LATE"]
    [nobody] = [i for i in items if i.payload == "nobody's"]
    [late_code] = [i for i in items if i.tool_use_id == "late-bash"]
    assert (late.parent_item_id, late_code.parent_item_id) == (spawn.item_id, spawn.item_id)
    assert nobody.parent_item_id is None


FIXTURE = (
    Path(__file__).parents[4] / "base/lm/harness/claude/transcript/tests/fixtures/basic/main.jsonl"
)
SESSION = "5914ebdf-bbec-47c8-bcd6-5a6da081f08a"
BASIC_USER = "3c3d2de5-c479-52b7-91a6-fce4e4d26db9"


# meiva:note
# The text of the last assistant reply in the recorded turn: the reply the page and the user are to get.
def _recorded_reply() -> str:
    texts: list[str] = []
    for line in FIXTURE.read_text().splitlines():
        row = cast("dict[str, Any]", json.loads(line))
        message = cast("dict[str, Any]", row.get("message") or {})
        for block in cast("list[dict[str, Any]]", message.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block["text"]))
    return texts[-1]


REPLY_TEXT = _recorded_reply()


# meiva:note
# The CLI as far as the record goes: the input it takes is written as the rows the CLI writes for it.
class RecordingCli:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.turns = 0

    def write(self, key: str) -> None:
        self.turns += 1
        n = self.turns
        text = FIXTURE.read_text().replace(BASIC_USER, "USERUUID")
        text = re.sub(
            r"\b([0-9a-f]{8})(-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
            lambda m: f"{n:08x}{m.group(2)}",
            text,
        )
        text = re.sub(r"toolu_\w+", lambda m: f"{m.group(0)}x{n}", text)
        now = datetime.now(UTC).isoformat()
        text = re.sub(r'"timestamp":"[^"]+"', f'"timestamp":"{now}"', text)
        with self.path.open("a") as handle:
            handle.write(text.replace("USERUUID", key_uuid(key)))

    def __call__(self, inputs: Sequence[HarnessInput]):
        keys = [item.key for item in inputs]
        self.write(keys[0])
        return in_a_turn(keys, REPLY_TEXT)


def _record_wired(
    tmp_path: Path,
    engine: FakeEngine,
    pool: AsyncConnectionPool,
    resources: HostedTurnResources,
    gate: ProcessDbGate,
    live: LiveEvents,
    agent_id: int,
) -> Runtime[MeivaContext]:
    base = runtime(engine, ops_pool=pool, hosted_resources=resources, publisher=live.publisher)
    return Runtime(
        context=replace(
            base.context,
            harness=Harness.CLAUDE_CODE,
            meiva_home=tmp_path / "home",
            environ={"HOME": str(tmp_path)},
            db=Database.from_settings(gate=gate),
            bus=EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
            agent=AgentSlices.resolve({"user_delivery_mode": "naive"}, default_reader=defaults()),
        )
    )


async def test_a_claude_turn_reaches_the_page_the_user_and_the_inbox_through_the_record(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id = agent_row(db_conn)
    db_conn.execute("UPDATE agents_meta SET harness='claude_code' WHERE id=%s", (agent_id,))
    db_conn.commit()
    row = insert_row(db_conn, agent_id, "chat", "user", "hello")
    workspace = workspace_dir(agent_id, home=tmp_path / "home")
    folder = tmp_path / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    folder.mkdir(parents=True)
    cli = RecordingCli(folder / f"{SESSION}.jsonl")
    engine = FakeEngine(script=cli)
    engine.session_id = SESSION
    rt = _record_wired(
        tmp_path, engine, aops_pool, hosted_resources, database_gate, live_events, agent_id
    )

    command = await harness_turn_node(AgentState(), rt, config(agent_id))

    assert command.goto == "claim"
    assert row_status(db_conn, row) == "done", "the input is settled without a timeline write"
    async with aops_pool.connection() as conn:
        assert await aload_harness_timeline_items(conn, agent_id) == [], "no stored rows"
    _assert_page_saw_the_message(
        [json.loads(payload) for payload in await live_events.payloads()], row
    )
    published = _publications(db_conn, agent_id)
    assert len(published) == 1 and published[0][1] == REPLY_TEXT
    assert re.fullmatch(r"harness:claude:[0-9a-f-]{36}", published[0][0])
    restarted = FakeEngine(script=lambda _inputs: [])
    restarted.session_id = SESSION
    again = _record_wired(
        tmp_path, restarted, aops_pool, hosted_resources, database_gate, live_events, agent_id
    )
    _ = await harness_turn_node(AgentState(), again, config(agent_id))
    assert _publications(db_conn, agent_id) == published


def _assert_page_saw_the_message(events: list[dict[str, Any]], row: int) -> None:
    snapshots = [e for e in events if e.get("role") == "timeline_snapshot"]
    shown = {item["item_id"]: item for e in snapshots for item in e["items"]}
    first = next(iter(shown.values()))
    assert (first["kind"], first["inbound_id"]) == ("inbound_chat", row), (
        "the pending strip can clear"
    )
    assert all(re.fullmatch(r"h\d+\.\d+", item_id) for item_id in shown)
    committed = [e["inbound_id"] for e in events if e.get("role") == "inbound_committed"]
    assert committed == [row]


def _publications(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[str, str]]:
    db_conn.commit()
    rows = db_conn.execute(
        "SELECT source_key, content FROM agent_publications WHERE agent_id=%s ORDER BY id",
        (agent_id,),
    ).fetchall()
    return [(str(key), str(content)) for key, content in rows]


def _claude_agent(tmp_path: Path, db_conn: psycopg.Connection) -> tuple[int, Path]:
    agent_id = agent_row(db_conn)
    db_conn.execute("UPDATE agents_meta SET harness='claude_code' WHERE id=%s", (agent_id,))
    db_conn.commit()
    workspace = workspace_dir(agent_id, home=tmp_path / "home")
    folder = tmp_path / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    folder.mkdir(parents=True)
    return agent_id, folder


def _engine(script: Any = None) -> FakeEngine:
    engine = FakeEngine(script=script or (lambda _inputs: []))
    engine.session_id = SESSION
    return engine


async def test_the_switch_from_the_old_keys_does_not_resend_and_what_follows_is_delivered_by_key(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id, folder = _claude_agent(tmp_path, db_conn)
    cli = RecordingCli(folder / f"{SESSION}.jsonl")
    cli.write("inbound-100")
    db_conn.execute(
        "INSERT INTO agent_publications (agent_id, source_key, mode, content, suggestions) "
        "VALUES (%s, 'harness:h5.0', 'naive', %s, '[]'::jsonb)",
        (agent_id, REPLY_TEXT),
    )
    db_conn.commit()
    old = _publications(db_conn, agent_id)

    def run(engine: FakeEngine):
        rt = _record_wired(
            tmp_path, engine, aops_pool, hosted_resources, database_gate, live_events, agent_id
        )
        return harness_turn_node(AgentState(), rt, config(agent_id))

    _ = await run(_engine())
    assert _publications(db_conn, agent_id) == old, (
        "the reply the old keys delivered is not sent again"
    )
    for _turn in range(2):
        _ = insert_row(db_conn, agent_id, "chat", "user", "again")
        _ = await run(_engine(cli))
    new = _publications(db_conn, agent_id)
    assert [content for _key, content in new] == [REPLY_TEXT] * 3, (
        "two replies with the same text both arrive"
    )
    assert len({key for key, _content in new}) == 3
    cli.write("inbound-200")
    _ = await run(_engine())
    assert len(_publications(db_conn, agent_id)) == 4
    _ = await run(_engine())
    assert len(_publications(db_conn, agent_id)) == 4, "and only once"


async def test_a_turn_with_no_record_file_to_show_for_it_fails_the_way_a_setup_failure_does(
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    agent_id, _folder = _claude_agent(tmp_path, db_conn)
    _ = insert_row(db_conn, agent_id, "chat", "user", "hello")

    def unseen(inputs: Sequence[HarnessInput]) -> Sequence[BridgeEvent]:
        return in_a_turn([item.key for item in inputs], "a reply nobody can see")

    engine = _engine(unseen)
    rt = _record_wired(
        tmp_path, engine, aops_pool, hosted_resources, database_gate, live_events, agent_id
    )

    with pytest.raises(FatalLLMStreamError) as failed:
        _ = await harness_turn_node(AgentState(), rt, config(agent_id))

    message = str(failed.value)
    assert SESSION in message and str(tmp_path / ".claude/projects") in message
