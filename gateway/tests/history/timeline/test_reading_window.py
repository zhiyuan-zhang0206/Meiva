from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Annotated, Literal, Protocol, TypedDict, cast

import psycopg
import pytest
import zstandard
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.channels.delta import DeltaChannel
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from base.agents.tests.langgraph_views import NodeGraphBuilder
from base.api_contracts.reading import (
    NativeReadingLocator,
    RecordReadingLocator,
    RecordReadingSource,
    decode_reading_locator,
    encode_reading_locator,
    encode_reading_source,
)
from base.db import create_agent
from base.lm.harness.registry import FileSessionRegistry, SessionRecord
from base.paths import harness_sessions_dir
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    anonymous_gateway_client,
    gateway_test_client,
    reconfigure,
)
from gateway.tests.history.test_timeline_record_read import ScriptedRunner
from gateway.tests.history.test_timeline_record_read_codex import RecordOperationRunner
from gateway.tests.support.checkpoints.stored import put_checkpoint
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable


@pytest.fixture
def client(gateway_data_plane: None) -> Iterator[GatewayTestClient]:
    _ = gateway_data_plane
    with gateway_test_client() as value:
        yield value


def agent(conn: psycopg.Connection, harness: str) -> int:
    value = create_agent(conn)
    _ = conn.execute(
        "INSERT INTO agents_meta (id, machine, status, harness) VALUES (%s, 'macmini', 'idling', %s)",
        (value, harness),
    )
    conn.commit()
    return value


def register(home: Path, agent_id: int, harness: str, session: str, cwd: Path) -> None:
    FileSessionRegistry(harness_sessions_dir(home=home)).save(
        SessionRecord(
            agent_id=agent_id,
            harness=harness,
            session_id=session,
            cwd=str(cwd),
            model="test",
            created_at="2026-11-18T00:00:00Z",
            updated_at="2026-11-18T00:00:00Z",
            generation=1,
        )
    )


def write_record(root: Path, harness: str, session: str, packed: bool) -> dict[str, str]:
    fixtures = Path(__file__).parents[4] / "base/lm/harness"
    if harness == "codex":
        path = root / "codex/sessions/2026/11/18" / f"rollout-test-{session}.jsonl"
        raw = (fixtures / "codex/transcript/tests/fixtures/rollout/session.jsonl").read_bytes()
        if packed:
            path = path.with_suffix(".jsonl.zst")
    elif harness == "claude_code":
        path = root / ".claude/projects/test" / f"{session}.jsonl"
        raw = (fixtures / "claude/transcript/tests/fixtures/basic/main.jsonl").read_bytes()
    else:
        path = (
            root
            / f".gemini/antigravity-cli/brain/{session}/.system_generated/logs/transcript_full.jsonl"
        )
        raw = b"".join(
            (
                json.dumps(
                    {
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "step_index": index,
                        "created_at": "2026-11-18T00:00:00Z",
                        "content": f"planner {index}",
                        "tool_calls": [],
                    }
                )
                + "\n"
            ).encode()
            for index in range(4)
        )
    path.parent.mkdir(parents=True)
    _ = path.write_bytes(zstandard.ZstdCompressor().compress(raw) if packed else raw)
    return {"HOME": str(root), "CODEX_HOME": str(root / "codex")}


@pytest.mark.parametrize(
    "harness,packed",
    [("codex", False), ("codex", True), ("claude_code", False), ("antigravity", False)],
)
def test_actual_source_http_anchor_paging_and_registry_identity(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
    tmp_path: Path,
    harness: str,
    packed: bool,
) -> None:
    selected = agent(db_conn, harness)
    home, session = tmp_path / "home", "reading-session"
    register(home, selected, harness, session, tmp_path)
    environ = write_record(tmp_path, harness, session, packed)
    with ConnectionPool[psycopg.Connection[TupleRow]](db_url, min_size=1, max_size=1) as pool:
        client.app.state.rpc = RecordOperationRunner(home, environ, pool)
        head = client.get(f"/api/agents/{selected}/timeline")
        assert head.status_code == 200, head.text
        items = head.json()["items"]
        target = next(item for item in items if item["kind"] == "agent_chat")
        token = target["reading_locator"]
        result = client.get(
            f"/api/agents/{selected}/timeline/window", params={"anchor": token, "limit": 1}
        )
        assert result.status_code == 200, result.text
        body = result.json()
        assert body["anchor_item_id"] == target["item_id"]
        assert target["item_id"] in [item["item_id"] for item in body["items"]]
        assert body["reading_source"]
        walk_pages(client, selected, body["newer_cursor"], "after", "newer_cursor")
        walk_pages(client, selected, body["older_cursor"], "before", "older_cursor")
        locator = decode_reading_locator(token)
        assert isinstance(locator, RecordReadingLocator)
        missing = locator.model_copy(update={"block": 9999})
        rejected = client.get(
            f"/api/agents/{selected}/timeline/window",
            params={"anchor": encode_reading_locator(missing)},
        )
        assert rejected.status_code == 410 and rejected.json()["reading_reason"] == "identity"
        register(home, selected, harness, "replacement-session", tmp_path)
        replaced = client.get(f"/api/agents/{selected}/timeline/window", params={"anchor": token})
        assert replaced.status_code == 410 and replaced.json()["reading_reason"] == "source"


def walk_pages(
    client: GatewayTestClient, selected: int, cursor: str | None, direction: str, edge: str
) -> None:
    for _ in range(30):
        if cursor is None:
            return
        page = client.get(
            f"/api/agents/{selected}/timeline/window", params={direction: cursor, "limit": 1}
        )
        assert page.status_code == 200, page.text
        following = page.json()[edge]
        assert following is None or following != cursor
        cursor = following
    pytest.fail("source-issued reading cursors did not terminate")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"anchor": "bad"},
        {"anchor": "x", "before": "x"},
        {"before": "x", "after": "x"},
        {"anchor": "x", "limit": 0},
        {"anchor": "x", "limit": 1001},
    ],
)
def test_window_rejects_malformed_or_mixed_selectors(
    client: GatewayTestClient, params: dict[str, str | int]
) -> None:
    assert client.get("/api/agents/42/timeline/window", params=params).status_code == 422


def test_an_old_reader_cannot_report_its_head_as_a_window(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    selected = agent(db_conn, "codex")
    token = encode_reading_locator(
        RecordReadingLocator(agent_id=selected, harness="codex", session_id="s", offset=0, block=0)
    )
    client.app.state.rpc = ScriptedRunner({"items": [], "has_more": False, "size": 0})
    response = client.get(f"/api/agents/{selected}/timeline/window", params={"anchor": token})
    assert response.status_code == 503


@pytest.mark.parametrize("wrong", ["empty", "session"])
def test_capability_flag_does_not_substitute_for_source_or_membership(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    wrong: str,
) -> None:
    selected = agent(db_conn, "codex")
    locator = RecordReadingLocator(
        agent_id=selected, harness="codex", session_id="s", offset=0, block=0
    )
    source = RecordReadingSource(
        agent_id=selected, harness="codex", session_id="s" if wrong == "empty" else "other"
    )
    client.app.state.rpc = ScriptedRunner(
        {
            "items": [],
            "has_more": False,
            "size": 0,
            "reading_applied": True,
            "reading_source": encode_reading_source(source),
            "anchor_item_id": "h0.0",
        }
    )
    response = client.get(
        f"/api/agents/{selected}/timeline/window",
        params={"anchor": encode_reading_locator(locator)},
    )
    assert response.status_code == 503


def test_native_current_to_retained_matches_stable_identity_in_actual_saver(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
) -> None:
    selected = agent(db_conn, "meiva")
    reconfigure(client, timeline_compact_history=-1)
    messages: list[BaseMessage] = [
        SystemMessage(content="system"),
        HumanMessage(id="human", content="question"),
        AIMessage(id="reply", content="saved reply"),
    ]
    _ = put_checkpoint(db_url, selected, messages, version="1")
    head = client.get(f"/api/agents/{selected}/timeline")
    assert head.status_code == 200, head.text
    token = next(
        item["reading_locator"]
        for item in head.json()["items"]
        if item["source_message_id"] == "reply"
    )
    boundary = put_checkpoint(db_url, selected, messages, version="2", boundary=True)
    _ = put_checkpoint(
        db_url,
        selected,
        [SystemMessage(content="system"), AIMessage(id="new", content="new reply")],
        version="3",
    )
    result = client.get(
        f"/api/agents/{selected}/timeline/window", params={"anchor": token, "limit": 1}
    )
    assert result.status_code == 200, result.text
    saved = result.json()
    assert saved["anchor_item_id"].startswith(f"s1.{boundary}.")
    assert [item["payload"] for item in saved["items"]] == ["saved reply"]
    next_page = client.get(
        f"/api/agents/{selected}/timeline/window",
        params={"after": saved["newer_cursor"], "limit": 10},
    )
    assert next_page.status_code == 200, next_page.text
    assert "new reply" in [item["payload"] for item in next_page.json()["items"]]
    wrong = decode_reading_locator(token)
    assert isinstance(wrong, NativeReadingLocator)
    mismatch = wrong.model_copy(update={"message_id": "same-position-different-identity"})
    assert (
        client.get(
            f"/api/agents/{selected}/timeline/window",
            params={"anchor": encode_reading_locator(mismatch)},
        ).status_code
        == 410
    )
    reconfigure(client, timeline_compact_history=0)
    assert (
        client.get(f"/api/agents/{selected}/timeline/window", params={"anchor": token}).status_code
        == 410
    )


def test_boundary_stamp_before_new_current_commit_keeps_the_old_domain(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
) -> None:
    selected = agent(db_conn, "meiva")
    reconfigure(client, timeline_compact_history=-1)
    messages: list[BaseMessage] = [
        SystemMessage(content="system"),
        AIMessage(id="old", content="saved"),
    ]
    boundary = put_checkpoint(db_url, selected, messages, version="1", boundary=True)
    head = client.get(f"/api/agents/{selected}/timeline")
    assert head.status_code == 200, head.text
    row = next(item for item in head.json()["items"] if item["source_message_id"] == "old")
    locator = decode_reading_locator(row["reading_locator"])
    assert isinstance(locator, NativeReadingLocator)
    assert locator.checkpoint_id == boundary and locator.epoch == "initial"
    reconfigure(client, timeline_compact_history=0)
    active = client.get(
        f"/api/agents/{selected}/timeline/window", params={"anchor": row["reading_locator"]}
    )
    assert active.status_code == 200, active.text
    reconfigure(client, timeline_compact_history=-1)
    _ = put_checkpoint(
        db_url,
        selected,
        [SystemMessage(content="system"), AIMessage(id="new", content="new")],
        version="2",
    )
    restored = client.get(
        f"/api/agents/{selected}/timeline/window", params={"anchor": row["reading_locator"]}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["anchor_item_id"].startswith(f"s1.{boundary}.")


def test_source_budget_reason_survives_the_actual_http_envelope(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
    tmp_path: Path,
) -> None:
    selected = agent(db_conn, "codex")
    session, home = "budget-session", tmp_path / "home"
    register(home, selected, "codex", session, tmp_path)
    environ = write_record(tmp_path, "codex", session, False)
    path = tmp_path / "codex/sessions/2026/11/18" / f"rollout-test-{session}.jsonl"
    _ = path.write_text(
        json.dumps(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "x" * (17 * 1024 * 1024)}],
                },
            }
        )
        + "\n"
    )
    token = encode_reading_locator(
        RecordReadingLocator(
            agent_id=selected, harness="codex", session_id=session, offset=0, block=0
        )
    )
    with ConnectionPool[psycopg.Connection[TupleRow]](db_url, min_size=1, max_size=1) as pool:
        client.app.state.rpc = RecordOperationRunner(home, environ, pool)
        response = client.get(f"/api/agents/{selected}/timeline/window", params={"anchor": token})
    assert response.status_code == 410, response.text
    assert response.json()["code"] == "reading_source_unavailable"
    assert response.json()["reading_reason"] == "budget"


def test_isolated_caller_is_denied_before_the_source_read(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
) -> None:
    selected = agent(db_conn, "codex")
    _ = db_conn.execute(
        "UPDATE agents_meta SET config_overlay = '{\"eval_isolation\":true}'::jsonb WHERE id=%s",
        (selected,),
    )
    db_conn.commit()
    runner = ScriptedRunner({"items": [], "has_more": False, "size": 0})
    client.app.state.rpc = runner
    token = encode_reading_locator(
        RecordReadingLocator(agent_id=selected, harness="codex", session_id="s", offset=0, block=0)
    )
    response = client.get(
        f"/api/agents/{selected}/timeline/window",
        params={"anchor": token, "caller": f"agent:{selected}"},
    )
    assert response.status_code == 403
    assert runner.calls == []


def test_native_proven_empty_current_source_finishes_without_a_repeated_cursor(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
) -> None:
    selected = agent(db_conn, "meiva")
    reconfigure(client, timeline_compact_history=-1)
    boundary = put_checkpoint(
        db_url,
        selected,
        [SystemMessage(content="system"), AIMessage(id="old", content="saved")],
        version="1",
        boundary=True,
    )
    _ = put_checkpoint(db_url, selected, [], version="2")
    locator = NativeReadingLocator(
        agent_id=selected, epoch="initial", checkpoint_id=boundary, message_id="old", block=0
    )
    first = client.get(
        f"/api/agents/{selected}/timeline/window",
        params={"anchor": encode_reading_locator(locator)},
    )
    assert first.status_code == 200, first.text
    cursor = first.json()["newer_cursor"]
    last = client.get(f"/api/agents/{selected}/timeline/window", params={"after": cursor})
    assert last.status_code == 200, last.text
    assert last.json()["items"] == [] and last.json()["newer_cursor"] is None
    head = client.get(f"/api/agents/{selected}/timeline")
    assert head.status_code == 200 and head.json()["items"] == []
    assert head.json()["reading_source"] == last.json()["reading_source"]


def append_delta(
    state: list[BaseMessage], writes: Sequence[list[BaseMessage]]
) -> list[BaseMessage]:
    return [*state, *(message for batch in writes for message in batch)]


class DeltaState(TypedDict):
    messages: Annotated[
        list[BaseMessage], DeltaChannel[list[BaseMessage]](append_delta, snapshot_frequency=1000)
    ]


class DeltaRunnable(Protocol):
    def invoke(self, input: DeltaState, config: RunnableConfig) -> object: ...


type DeltaBuilder = NodeGraphBuilder[
    Callable[[DeltaState], dict[str, list[BaseMessage]]], DeltaRunnable
]


def test_reading_source_reconstructs_a_real_delta_channel_checkpoint(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
) -> None:
    selected = agent(db_conn, "meiva")

    def emit(_state: DeltaState) -> dict[str, list[BaseMessage]]:
        return {"messages": [AIMessage(id="delta-reply", content="delta body")]}

    graph = cast(DeltaBuilder, StateGraph[DeltaState, None, DeltaState, DeltaState](DeltaState))
    _ = graph.add_node("emit", emit)
    _ = graph.add_edge(START, "emit")
    _ = graph.add_edge("emit", END)
    with PostgresSaver.from_conn_string(db_url) as saver:
        saver.setup()
        runnable = graph.compile(checkpointer=saver)
        _ = runnable.invoke(
            {"messages": [HumanMessage(id="delta-input", content="question")]},
            {"configurable": {"thread_id": str(selected)}},
        )
    head = client.get(f"/api/agents/{selected}/timeline")
    assert head.status_code == 200, head.text
    row = next(item for item in head.json()["items"] if item["source_message_id"] == "delta-reply")
    restored = client.get(
        f"/api/agents/{selected}/timeline/window", params={"anchor": row["reading_locator"]}
    )
    assert restored.status_code == 200, restored.text
    assert restored.json()["anchor_item_id"] == row["item_id"]


def test_nonprogressing_rpc_cursor_is_rejected(
    client: GatewayTestClient, db_conn: psycopg.Connection
) -> None:
    selected = agent(db_conn, "codex")
    locator = RecordReadingLocator(
        agent_id=selected, harness="codex", session_id="s", offset=0, block=0
    )
    token = encode_reading_locator(locator)
    source = RecordReadingSource(agent_id=selected, harness="codex", session_id="s")
    client.app.state.rpc = ScriptedRunner(
        {
            "items": [
                {
                    "item_id": "h0.0",
                    "kind": "agent_chat",
                    "payload": "same",
                    "reading_locator": token,
                }
            ],
            "has_more": False,
            "size": 10,
            "reading_applied": True,
            "reading_source": encode_reading_source(source),
            "newer_cursor": token,
        }
    )
    response = client.get(f"/api/agents/{selected}/timeline/window", params={"after": token})
    assert response.status_code == 503


@pytest.mark.parametrize("initial,final", [("idling", "running"), ("running", "idling")])
def test_window_settles_activity_after_actual_record_rpc(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
    tmp_path: Path,
    initial: str,
    final: str,
) -> None:
    selected = agent(db_conn, "codex")
    session, home = "pending-window", tmp_path / "home"
    register(home, selected, "codex", session, tmp_path)
    environ = write_record(tmp_path, "codex", session, False)
    record = next((tmp_path / "codex").rglob("*.jsonl"))
    pending = next(
        json.loads(row) for row in record.read_text().splitlines() if '"CommandExecution"' in row
    )
    pending["payload"]["item"]["status"] = "inProgress"
    _ = record.write_text(json.dumps(pending) + "\n" + '{"partial":')
    _ = db_conn.execute("UPDATE agents_meta SET status=%s WHERE id=%s", (initial, selected))
    db_conn.commit()

    def transition() -> None:
        _ = db_conn.execute("UPDATE agents_meta SET status=%s WHERE id=%s", (final, selected))
        db_conn.commit()

    with ConnectionPool[psycopg.Connection[TupleRow]](db_url, min_size=1, max_size=1) as pool:
        runner = RecordOperationRunner(home, environ, pool, on_dispatch=transition)
        client.app.state.rpc = runner
        token = encode_reading_locator(
            RecordReadingLocator(
                agent_id=selected, harness="codex", session_id=session, offset=0, block=0
            )
        )
        response = client.get(f"/api/agents/{selected}/timeline/window", params={"anchor": token})
        assert response.status_code == 200, response.text
        assert runner.raw_pending
        [shown] = response.json()["items"]
        assert shown["activity_pending"] is (True if final == "running" else None)


def test_repeated_selectors_and_missing_agent_do_not_dispatch(client: GatewayTestClient) -> None:
    runner = ScriptedRunner({"items": [], "has_more": False, "size": 0})
    client.app.state.rpc = runner
    locator = RecordReadingLocator(
        agent_id=9999999, harness="codex", session_id="s", offset=0, block=0
    )
    token = encode_reading_locator(locator)
    repeated = client.get(
        "/api/agents/9999999/timeline/window", params=[("anchor", token), ("anchor", token)]
    )
    assert repeated.status_code == 422
    missing = client.get("/api/agents/9999999/timeline/window", params={"anchor": token})
    assert missing.status_code == 404
    assert runner.calls == []


@pytest.mark.parametrize(
    "failure,status",
    [(ClusterOpUnreachable("offline"), 503), (ClusterOpFailed({"error": "unreadable"}), 502)],
)
def test_window_preserves_source_failure_without_retry(
    client: GatewayTestClient, db_conn: psycopg.Connection, failure: Exception, status: int
) -> None:
    selected = agent(db_conn, "codex")
    runner = ScriptedRunner(failure)
    client.app.state.rpc = runner
    token = encode_reading_locator(
        RecordReadingLocator(agent_id=selected, harness="codex", session_id="s", offset=0, block=0)
    )
    response = client.get(f"/api/agents/{selected}/timeline/window", params={"anchor": token})
    assert response.status_code == status, response.text
    assert len(runner.calls) == 1


def test_window_uses_existing_authentication() -> None:
    with anonymous_gateway_client(client=("127.0.0.1", 5000)) as anonymous:
        response = anonymous.get("/api/agents/42/timeline/window", params={"anchor": "bad"})
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "authentication_required"


@pytest.mark.parametrize("harness", ["claude_code", "antigravity"])
def test_inactive_process_does_not_qualify_incomplete_source_text(
    client: GatewayTestClient,
    db_conn: psycopg.Connection,
    db_url: str,
    tmp_path: Path,
    harness: Literal["claude_code", "antigravity"],
) -> None:
    selected = agent(db_conn, harness)
    session, home = "incomplete-window", tmp_path / "home"
    register(home, selected, harness, session, tmp_path)
    environ = write_record(tmp_path, harness, session, False)
    partial: dict[str, object]
    if harness == "claude_code":
        record = next((tmp_path / ".claude").rglob("*.jsonl"))
        partial = {
            "type": "assistant",
            "uuid": "partial",
            "isAbortedMidStream": True,
            "message": {"id": "partial", "content": [{"type": "text", "text": "unfinished"}]},
        }
    else:
        record = next((tmp_path / ".gemini").rglob("*.jsonl"))
        partial = {
            "type": "PLANNER_RESPONSE",
            "status": "GENERATING",
            "created_at": "2026-11-18T00:00:00Z",
            "content": "unfinished",
            "tool_calls": [],
        }
    _ = record.write_text(json.dumps(partial) + "\n")
    with ConnectionPool[psycopg.Connection[TupleRow]](db_url, min_size=1, max_size=1) as pool:
        client.app.state.rpc = RecordOperationRunner(home, environ, pool)
        token = encode_reading_locator(
            RecordReadingLocator(
                agent_id=selected, harness=harness, session_id=session, offset=0, block=0
            )
        )
        response = client.get(f"/api/agents/{selected}/timeline/window", params={"anchor": token})
        assert response.status_code == 410, response.text
        assert response.json()["reading_reason"] == "identity"
