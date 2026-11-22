import asyncio
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from langgraph.runtime import Runtime
from psycopg_pool import AsyncConnectionPool

from agent.graph.claim.harness_read_sink import ReadTimelineSink
from agent.graph.claim.harness_turn import harness_turn_node
from agent.graph.claim.tests.claim_support import snapshot_items, timeline_items
from agent.graph.claim.tests.harness_support import (
    FakeEngine,
    RuntimeFixture,
    Script,
    in_a_turn,
    input_key,
    insert_row,
    publications,
    read_recorded_items,
    row_status,
    wait_until,
)
from agent.graph.claim.tests.harness_support import (
    harness_runtime as harness_runtime,
)
from agent.graph.claim.tests.test_harness_turn import config, runtime
from agent.graph.llm_errors import FatalLLMStreamError
from agent.ownership.tests.ownership_support import agent_row
from agent.state import AgentState
from base.agents.context import HostedTurnResources, MeivaContext
from base.agents.history.transcript_timeline.items import ReplyPolicy
from base.agents.tests.langgraph_views import command_update
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS
from base.clock.tests.fakes import system_clock
from base.clock.tests.seeded_rng import seeded_rng
from base.config.tests.configuration import defaults
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.events.live.projection import TimelineSnapshot
from base.events.live.tests.live_capture import LiveEvents
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.boundary.untyped import object_of
from base.host.env.agent_slices import AgentSlices
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    HarnessInput,
    InputInContext,
    TextDelta,
    ToolUse,
    TurnOrigin,
    TurnStarted,
)
from base.lm.harness.antigravity.timing_store import store_timings
from base.lm.harness.antigravity.trajectory import Trajectory, parse_trajectory
from base.lm.harness.claude.delivery import key_uuid
from base.paths import meiva_home, workspace_dir
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
        harness_inputs=[{"key": "human-request", "prompt": "Do the work"}],
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
    state = AgentState(harness_inputs=[{"key": "inbound-10", "prompt": "hello from user"}])
    cmd_valid = await harness_turn_node(
        state, harness_runtime(engine_valid, agent_id=agent_id), config(agent_id)
    )
    assert cmd_valid.goto == "claim"
    assert command_update(cmd_valid)["harness_inputs"] == []
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
    state = AgentState(harness_inputs=[{"key": "human-request", "prompt": "Work"}])
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
        row = object_of(json.loads, line)
        message = row.get("message") if is_object_dict(row) else None
        blocks = message.get("content") if is_object_dict(message) else None
        for block in blocks if is_object_list(blocks) else []:
            if is_object_dict(block) and block.get("type") == "text":
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
    _assert_page_saw_the_message(await live_events.object_events(), row)
    published = publications(db_conn, agent_id)
    assert len(published) == 1 and published[0][1] == REPLY_TEXT
    assert re.fullmatch(r"harness:claude:[0-9a-f-]{36}", published[0][0])
    restarted = FakeEngine(script=lambda _inputs: [])
    restarted.session_id = SESSION
    again = _record_wired(
        tmp_path, restarted, aops_pool, hosted_resources, database_gate, live_events, agent_id
    )
    _ = await harness_turn_node(AgentState(), again, config(agent_id))
    assert publications(db_conn, agent_id) == published


def _assert_page_saw_the_message(events: Sequence[Mapping[str, object]], row: int) -> None:
    snapshots = [e for e in events if e.get("role") == "timeline_snapshot"]
    shown = timeline_items(snapshots)
    first = next(iter(shown.values()))
    assert (first["kind"], first["inbound_id"]) == ("inbound_chat", row), (
        "the pending strip can clear"
    )
    assert all(re.fullmatch(r"h\d+\.\d+", item_id) for item_id in shown)
    committed = [e["inbound_id"] for e in events if e.get("role") == "inbound_committed"]
    assert committed == [row]


def _claude_agent(tmp_path: Path, db_conn: psycopg.Connection) -> tuple[int, Path]:
    agent_id = agent_row(db_conn)
    db_conn.execute("UPDATE agents_meta SET harness='claude_code' WHERE id=%s", (agent_id,))
    db_conn.commit()
    workspace = workspace_dir(agent_id, home=tmp_path / "home")
    folder = tmp_path / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
    folder.mkdir(parents=True)
    return agent_id, folder


def _engine(script: Script | None = None) -> FakeEngine:
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
    old = publications(db_conn, agent_id)

    def run(engine: FakeEngine):
        rt = _record_wired(
            tmp_path, engine, aops_pool, hosted_resources, database_gate, live_events, agent_id
        )
        return harness_turn_node(AgentState(), rt, config(agent_id))

    _ = await run(_engine())
    assert publications(db_conn, agent_id) == old, (
        "the reply the old keys delivered is not sent again"
    )
    for _turn in range(2):
        _ = insert_row(db_conn, agent_id, "chat", "user", "again")
        _ = await run(_engine(cli))
    new = publications(db_conn, agent_id)
    assert [content for _key, content in new] == [REPLY_TEXT] * 3, (
        "two replies with the same text both arrive"
    )
    assert len({key for key, _content in new}) == 3
    cli.write("inbound-200")
    _ = await run(_engine())
    assert len(publications(db_conn, agent_id)) == 4
    _ = await run(_engine())
    assert len(publications(db_conn, agent_id)) == 4, "and only once"


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


def _prepare_pre_tool_transcript(
    harness: Harness,
    *,
    root: Path,
    workspace: Path,
    sid: str,
) -> tuple[dict[str, str], Path]:
    env = {
        **os.environ,
        "HOME": str(root),
        "CODEX_HOME": str(root / "codex"),
        "CLAUDE_CONFIG_DIR": str(root / ".claude"),
    }
    if harness is Harness.CLAUDE_CODE:
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(workspace))
        folder = root / ".claude/projects" / slug
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{sid}.jsonl"
        initial: list[dict[str, object]] = [
            {
                "type": "user",
                "uuid": "u0",
                "timestamp": "2026-11-19T08:00:00Z",
                "sessionId": sid,
                "cwd": str(workspace),
                "message": {"role": "user", "content": "run slow check"},
            }
        ]
    elif harness is Harness.CODEX:
        day = root / "codex/sessions/2026/11/19"
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"rollout-2026-11-19T08-00-00-{sid}.jsonl"
        initial = [
            {
                "timestamp": "2026-11-19T08:00:00Z",
                "ordinal": 0,
                "type": "session_meta",
                "payload": {"id": sid, "cwd": str(workspace)},
            },
            {
                "timestamp": "2026-11-19T08:00:00Z",
                "ordinal": 1,
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "t1"},
            },
        ]
    else:
        log_dir = root / ".gemini/antigravity-cli/brain" / sid / ".system_generated/logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "transcript_full.jsonl"
        initial = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-19T08:00:00Z",
                "content": "<USER_REQUEST>\nrun slow check\n</USER_REQUEST>",
            }
        ]
    path.write_text("\n".join(json.dumps(r) for r in initial) + "\n")
    return env, path


def _append_pre_tool_reply(
    harness: Harness,
    path: Path,
    *,
    workspace: Path,
    sid: str,
    text: str,
) -> None:
    if harness is Harness.CLAUDE_CODE:
        extra = [
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "2026-11-19T08:00:01Z",
                "sessionId": sid,
                "cwd": str(workspace),
                "message": {
                    "id": "msg1",
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                },
            },
            {
                "type": "assistant",
                "uuid": "a2",
                "timestamp": "2026-11-19T08:00:01Z",
                "sessionId": sid,
                "cwd": str(workspace),
                "message": {
                    "id": "msg1",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu1",
                            "name": "Bash",
                            "input": {"command": "sleep 4"},
                        }
                    ],
                },
            },
        ]
    elif harness is Harness.CODEX:
        extra = [
            {
                "timestamp": "2026-11-19T08:00:01Z",
                "ordinal": 2,
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": sid,
                    "turn_id": "t1",
                    "started_at_ms": 1795075201000,
                    "completed_at_ms": 1795075201100,
                    "item": {
                        "type": "AgentMessage",
                        "id": "msg1",
                        "phase": "commentary",
                        "content": [{"type": "Text", "text": text}],
                    },
                },
            },
        ]
    else:
        extra = [
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-19T08:00:01Z",
                "content": text,
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "sleep 4"}}],
            }
        ]
    with path.open("a", encoding="utf-8") as fh:
        for row in extra:
            fh.write(json.dumps(row) + "\n")


@pytest.mark.parametrize("harness", [Harness.CLAUDE_CODE, Harness.CODEX, Harness.ANTIGRAVITY])
async def test_external_harness_publishes_pre_tool_display_before_tool_completes(
    harness: Harness,
    tmp_path: Path,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    database_gate: ProcessDbGate,
    hosted_resources: HostedTurnResources,
    live_events: LiveEvents,
) -> None:
    tid = create_agent(db_conn)
    row_id = insert_row(db_conn, tid, "chat", "user", "run slow check")
    sid = f"sid-{harness.value}-{tid}"
    ack_text = "MEIVA_DISPLAY_TO_USER\nStarting slow check now."
    workspace = workspace_dir(tid, home=meiva_home())
    env, transcript_path = _prepare_pre_tool_transcript(
        harness, root=tmp_path, workspace=workspace, sid=sid
    )

    base = runtime(
        None,
        ops_pool=aops_pool,
        hosted_resources=hosted_resources,
        publisher=live_events.publisher,
        agent_id=tid,
        database_gate=database_gate,
    )
    ctx = replace(
        base.context,
        harness=harness,
        harness_bridge=SimpleNamespace(session_id=sid),
        db=Database.from_settings(gate=database_gate),
        environ=env,
        agent=AgentSlices.resolve(
            {"user_delivery_mode": "prefix", "fast_acknowledgement": True},
            default_reader=defaults(),
        ),
    )
    sink = ReadTimelineSink(
        ctx,
        tid,
        live_events.publisher,
        non_human_wake=False,
        session_id=lambda: sid,
    )
    flusher = asyncio.create_task(sink.flush_text())
    try:
        _append_pre_tool_reply(
            harness, transcript_path, workspace=workspace, sid=sid, text=ack_text
        )
        key = input_key(row_id)
        for ev in (
            TurnStarted("t1", TurnOrigin.INPUT, (key,), 1),
            InputInContext(key, "t1", 1),
            AssistantText("t1", ack_text),
            ToolUse("t1", "tc1", "Bash", {"command": "sleep 4"}),
        ):
            await sink.on_event(ev)

        def _published() -> bool:
            db_conn.commit()
            return bool(
                db_conn.execute(
                    "SELECT 1 FROM agent_publications WHERE agent_id=%s", (tid,)
                ).fetchone()
            )

        await wait_until(_published, "pre-tool publication flushed while tool still open")
        pubs = db_conn.execute(
            "SELECT mode, content FROM agent_publications WHERE agent_id=%s", (tid,)
        ).fetchall()
        assert pubs == [("prefix", "Starting slow check now.")]
        snapshots = [
            e for e in await live_events.object_events() if e.get("role") == "timeline_snapshot"
        ]
        assert snapshots and any(
            item.get("payload") == "Starting slow check now." for item in snapshot_items(snapshots)
        )
    finally:
        flusher.cancel()
        with pytest.raises(asyncio.CancelledError):
            await flusher


def _write_antigravity_row(path: Path, **fields: object) -> None:
    row = {"type": "PLANNER_RESPONSE", "status": "DONE", **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _reactive_timing_trajectory() -> Trajectory:
    step0 = {"type": "CORTEX_STEP_TYPE_USER_INPUT", "status": "CORTEX_STEP_STATUS_DONE"}
    step1 = {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": "CORTEX_STEP_STATUS_DONE",
        "metadata": {"createdAt": "2026-11-17T05:30:00.123Z"},
        "plannerResponse": {"thinking": "synthetic thought", "thinkingDuration": "30.265829s"},
    }
    return parse_trajectory(
        {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "numTotalSteps": 2,
            "trajectory": {"steps": [step0, step1]},
        }
    )


async def _collect_snapshots_until(
    live_events: LiveEvents, predicate: Callable[[list[TimelineSnapshot]], bool]
) -> list[TimelineSnapshot]:
    snapshots: list[TimelineSnapshot] = []
    async with asyncio.timeout(2.0):
        while True:
            snapshots.extend(await live_events.of_type(TimelineSnapshot))
            if predicate(snapshots):
                return snapshots
            await asyncio.sleep(0.02)


def _snapshot_items_of_kind(
    snaps: Sequence[TimelineSnapshot], kind: str
) -> list[dict[str, object]]:
    return [item for snap in snaps for item in snap.items if item.get("kind") == kind]


def _agent_publications(db_conn: psycopg.Connection, agent_id: int) -> list[tuple[object, ...]]:
    db_conn.commit()
    return db_conn.execute(
        "SELECT source_key, content FROM agent_publications WHERE agent_id=%s", (agent_id,)
    ).fetchall()


async def test_flush_text_wakes_on_disk_growth_while_stdout_is_silent_and_refreshes_timing(
    db_conn: psycopg.Connection,
    database_gate: ProcessDbGate,
    event_bus: EventBus,
    tmp_path: Path,
    live_events: LiveEvents,
) -> None:
    tid, session = agent_row(db_conn), "reactive-idle-silent-stdout"
    path = (
        tmp_path
        / ".gemini/antigravity-cli/brain"
        / session
        / ".system_generated/logs/transcript_full.jsonl"
    )
    path.parent.mkdir(parents=True)
    _write_antigravity_row(
        path,
        step_index=0,
        created_at="2026-11-17T05:29:59Z",
        content="MEIVA_HIDE_FROM_USER\ninitial",
    )
    refresh_calls = 0

    def on_refresh() -> None:
        nonlocal refresh_calls
        refresh_calls += 1

    ctx = MeivaContext(
        agent=AgentSlices.resolve({"user_delivery_mode": "prefix"}, default_reader=defaults()),
        harness=Harness.ANTIGRAVITY,
        meiva_home=tmp_path,
        db=Database.from_settings(gate=database_gate),
        bus=event_bus,
        clock_factory=system_clock,
        environ={"HOME": str(tmp_path)},
    )
    sink = ReadTimelineSink(
        ctx,
        tid,
        live_events.publisher,
        non_human_wake=False,
        session_id=lambda: session,
        poll_seconds=0.05,
        debounce_seconds=0.01,
        request_timing_refresh=on_refresh,
    )
    initial_offset = path.stat().st_size
    sink._from, sink._timing_start = initial_offset, 0
    assert sink._file is not None
    bytes_before_idle = sink._file.bytes_read

    flush_task = asyncio.create_task(sink.flush_text())
    try:
        await asyncio.sleep(0.16)
        assert sink._file.bytes_read == bytes_before_idle
        assert await live_events.events() == []
        assert refresh_calls == 0

        _write_antigravity_row(
            path,
            step_index=1,
            created_at="2026-11-17T05:30:00Z",
            thinking="synthetic thought",
            content="MEIVA_DISPLAY_TO_USER\nreactive reply",
        )
        snapshots = await _collect_snapshots_until(
            live_events,
            lambda snaps: any(
                i.get("payload") == "reactive reply"
                for i in _snapshot_items_of_kind(snaps, "agent_chat")
            ),
        )
        [reasoning] = _snapshot_items_of_kind(snapshots, "agent_reasoning")
        assert reasoning["reasoning_ms"] is None
        await wait_until(
            lambda: bool(_agent_publications(db_conn, tid)), "A1 persisted publication"
        )
        expected_pub = [(f"harness:antigravity:{session}:{initial_offset}", "reactive reply")]
        assert _agent_publications(db_conn, tid) == expected_pub
        assert refresh_calls == 1

        store_timings(path, _reactive_timing_trajectory())
        [update] = await _collect_snapshots_until(live_events, bool)
        assert len(update.items) == 1
        assert (
            update.items[0]["item_id"],
            update.items[0]["kind"],
            update.items[0]["reasoning_ms"],
        ) == (reasoning["item_id"], "agent_reasoning", 30265)
        assert _agent_publications(db_conn, tid) == expected_pub
    finally:
        flush_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await flush_task
