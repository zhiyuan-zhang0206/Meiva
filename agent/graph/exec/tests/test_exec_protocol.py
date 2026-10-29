# meiva:note
# Envelope + typed-blob protocol tests for the exec subprocess
# (`agent/execution/protocol.py`).
#
# The load-bearing assertions are the exact round-trips: the typed blob must
# reconstruct langchain messages field-for-field (AIMessage `usage_metadata`
# included — `convert_to_messages` loses it, which is why the protocol uses
# `JsonPlusSerializer` with the checkpoint allowlist) and `set` deltas, since
# the state snapshot (parent -> child) and the plugin state-update delta
# (child -> parent) both ride it.


from __future__ import annotations

import json
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeGuard
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

from agent.execution.protocol import (
    MAX_ENVELOPE_BYTES,
    REQUEST_VERSION,
    RESULT_VERSION,
    STALE_FILE_AGE_S,
    ResultPayload,
    dumps_typed,
    loads_typed,
    make_request_path,
    make_result_path,
    prune_stale,
    read_request,
    read_result,
    write_request,
    write_result,
)
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.paths import meiva_home, resolve_meiva_home
from meiva.tests.fixtures.pin_agent import exec_context

_NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def _context_description(agent_id: int | None) -> dict[str, Any]:
    return exec_context(agent_id).describe()


def _exec_envelope_events() -> list[dict[str, Any]]:
    """Read the durable telemetry mirror's exec-envelope rows."""
    from base.paths import logs_dir

    path = logs_dir(home=resolve_meiva_home()) / f"events-{datetime.now(UTC):%Y%m%d}.jsonl"
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event_name") == "exec_envelope":
            events.append(row)
    return events


# ── typed blob: exact round-trip (the reason this codec was chosen) ──────


def test_typed_blob_aimessage_exact_round_trip() -> None:
    """AIMessage with tool_calls / usage_metadata / response_metadata must
    come back field-for-field identical — the alternative
    (`convert_to_messages`) silently relocates `usage_metadata` into
    `additional_kwargs`, which would corrupt the snapshot's message history."""
    msg = AIMessage(
        content="",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "x = 1"}, "id": "tc-1", "type": "tool_call"}
        ],
        response_metadata={"model_provider": "anthropic", "stop_reason": "tool_use"},
        usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    )
    back = loads_typed(dumps_typed(msg))
    assert type(back) is AIMessage
    assert back.model_dump() == msg.model_dump()


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_message_list(value: object) -> TypeGuard[list[BaseMessage]]:
    return _is_object_list(value) and all(isinstance(item, BaseMessage) for item in value)


def _is_str_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def test_typed_blob_tool_and_human_messages_exact_round_trip() -> None:
    """ToolMessage additional_kwargs (meiva_msg_type) and a plain HumanMessage
    survive unchanged."""
    msgs = [
        ToolMessage(
            content="out", tool_call_id="tc-1", additional_kwargs={"meiva_msg_type": "exec_output"}
        ),
        HumanMessage(content="hi"),
    ]
    back = loads_typed(dumps_typed(msgs))
    assert _is_message_list(back)
    assert [type(m) for m in back] == [type(m) for m in msgs]
    assert [m.model_dump() for m in back] == [m.model_dump() for m in msgs]


def test_typed_blob_set_round_trip() -> None:
    """A custom plugin reducer delta can be a `set` — the codec must keep the
    type (the LangGraph reducer would break on a list)."""
    back = loads_typed(dumps_typed({"seen": {"a", "b"}}))
    assert _is_str_object_dict(back)
    assert back == {"seen": {"a", "b"}}
    assert isinstance(back["seen"], set)


# ── request envelope ──────────────────────────────────────────────────────


def _make_state_dump() -> dict[str, Any]:
    msg = AIMessage(
        content="snapshot message",
        tool_calls=[
            {"name": "execute_code", "args": {"code": "pass"}, "id": "tc-1", "type": "tool_call"}
        ],
        usage_metadata={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    )
    return {"messages": [msg], "halted": False}


def test_request_envelope_round_trip(tmp_path: Path) -> None:
    state = _make_state_dump()
    path = make_request_path(tmp_path, agent_id=7, request_id=UUID(int=1))
    write_request(
        path,
        code="print('hi')",
        context=_context_description(7),
        timeout_s=300.0,
        state=state,
        incarnation=None,
    )
    payload = read_request(path)
    assert payload.code == "print('hi')"
    assert payload.agent_id == 7
    assert payload.timeout_s == 300.0
    # The snapshot stays raw until materialized (task #3633 leg-2) — the decode
    # is exactly what the child defers.
    assert payload.state is None
    assert payload.state_raw is not None
    assert payload.materialize_state() == state  # exact, typed (messages back as instances)


def test_request_envelope_transfers_emit_size_and_serialize_time(
    tmp_path: Path, database: Database, machine_identity: MachineIdentityReader
) -> None:
    """Request writes and reads record the final envelope size and their own
    serialization cost in the durable event stream."""
    from base import telemetry
    from base.log import logger
    from base.telemetry.process_logging import add_event_sink

    events_before = len(_exec_envelope_events())
    pipeline = telemetry.build_pipeline(
        database=lambda: database, machine=machine_identity, home=meiva_home()
    )
    sink_id = add_event_sink(
        home=meiva_home(),
        process="test-exec-envelope",
        producer=lambda: pipeline,
        machine_reader=machine_identity.name,
    )
    try:
        with logger.contextualize(agent_id=7):
            path = make_request_path(tmp_path, agent_id=7, request_id=UUID(int=1))
            write_request(
                path,
                code="print('hi')",
                context=_context_description(7),
                timeout_s=1.0,
                state=_make_state_dump(),
                incarnation=None,
            )
            read_request(path)

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                telemetry.flush()
                if len(_exec_envelope_events()) >= events_before + 2:
                    break
                time.sleep(0.05)
    finally:
        logger.remove(sink_id)
        pipeline.stop(timeout=2)

    events = _exec_envelope_events()[events_before:]
    assert len(events) >= 2
    request_events = [event for event in events if event["attributes"]["envelope"] == "request"]
    by_op = {event["attributes"]["op"]: event["attributes"] for event in request_events}
    assert set(by_op) >= {"write", "read"}
    for attrs in by_op.values():
        assert attrs["size_bytes"] == path.stat().st_size
        assert isinstance(attrs["serialize_ms"], float)
        assert attrs["serialize_ms"] >= 0.0


def test_request_envelope_without_state(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=None, request_id=UUID(int=1))
    write_request(
        path,
        code="x = 1",
        context=_context_description(None),
        timeout_s=0.0,
        state=None,
        incarnation=None,
    )
    payload = read_request(path)
    assert payload.agent_id is None
    assert payload.state is None
    # no state keys in the raw JSON
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "state_b64" not in raw


def test_request_envelope_rejects_version_drift(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=7, request_id=UUID(int=1))
    write_request(
        path,
        code="x = 1",
        context=_context_description(7),
        timeout_s=1.0,
        state=None,
        incarnation=None,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["v"] = REQUEST_VERSION + 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        read_request(path)


# ── result envelope ───────────────────────────────────────────────────────


def test_result_envelope_round_trip(tmp_path: Path) -> None:
    payload = ResultPayload(
        kind="crashed",
        exc_type="ValueError",
        exc_msg="boom",
        full_traceback="Traceback...",
        state_update={"messages": [HumanMessage(content="note")]},
        sdk_calls=[{"method": "files.read", "count": 3}],
    )
    path = make_result_path(tmp_path, agent_id=7, result_id=UUID(int=1))
    write_result(path, payload)
    back = read_result(path)
    assert back.kind == "crashed"
    assert back.exc_type == "ValueError"
    assert back.exc_msg == "boom"
    assert back.full_traceback == "Traceback..."
    assert back.state_update == payload.state_update  # messages back as instances
    assert back.sdk_calls == payload.sdk_calls


def test_result_envelope_minimal(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7, result_id=UUID(int=1))
    write_result(path, ResultPayload(kind="done"))
    back = read_result(path)
    assert back.kind == "done"
    assert back.state_update is None


def test_result_envelope_without_sdk_calls_reads_as_old_format(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7, result_id=UUID(int=1))
    write_result(path, ResultPayload(kind="done"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["sdk_calls"]
    path.write_text(json.dumps(raw), encoding="utf-8")

    assert read_result(path).sdk_calls is None


def test_result_envelope_rejects_unknown_kind(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7, result_id=UUID(int=1))
    write_result(path, ResultPayload(kind="done"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["kind"] = "exploded"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown kind"):
        read_result(path)


def test_result_envelope_rejects_version_drift(tmp_path: Path) -> None:
    path = make_result_path(tmp_path, agent_id=7, result_id=UUID(int=1))
    write_result(path, ResultPayload(kind="done"))
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["v"] = RESULT_VERSION + 99
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        read_result(path)


# ── file hygiene ──────────────────────────────────────────────────────────


def test_envelope_files_are_owner_only(tmp_path: Path) -> None:
    request = make_request_path(tmp_path, agent_id=7, request_id=UUID(int=1))
    write_request(
        request,
        code="x = 1",
        context=_context_description(7),
        timeout_s=1.0,
        state=None,
        incarnation=None,
    )
    result = make_result_path(tmp_path, agent_id=7, result_id=UUID(int=2))
    write_result(result, ResultPayload(kind="done"))
    for path in (request, result):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600, f"{path.name} mode {oct(mode)} != 0600"
    agent_dir = request.parent
    assert stat.S_IMODE(agent_dir.stat().st_mode) == 0o700


def test_envelope_paths_are_named_by_their_call_identity(tmp_path: Path) -> None:
    request_id, result_id = UUID(int=1), UUID(int=2)
    request = make_request_path(tmp_path, agent_id=7, request_id=request_id)
    result = make_result_path(tmp_path, agent_id=7, result_id=result_id)
    assert request == tmp_path / "7" / f"req-{request_id.hex}.json"
    assert result == tmp_path / "7" / f"res-{result_id.hex}.json"


def test_pruning_judges_age_against_the_instant_it_is_given(tmp_path: Path) -> None:
    agent_dir = make_result_path(tmp_path, agent_id=7, result_id=UUID(int=1)).parent
    recent = agent_dir / "res-recent.json"
    recent.write_text("{}", encoding="utf-8")
    edge = _NOW.timestamp() - STALE_FILE_AGE_S
    os.utime(recent, (edge + 1, edge + 1))
    gone = agent_dir / "res-gone.json"
    gone.write_text("{}", encoding="utf-8")
    os.utime(gone, (edge - 1, edge - 1))

    prune_stale(tmp_path, 7, now=_NOW)

    assert recent.exists()
    assert not gone.exists()


def test_result_write_fails_on_unknown_kind(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown result kind"):
        result_path = make_result_path(tmp_path, agent_id=7, result_id=UUID(int=1))
        write_result(result_path, ResultPayload(kind="nope"))  # type: ignore[arg-type]


def test_stale_request_evidence_retained_while_result_is_pruned(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=7, request_id=UUID(int=1))
    old_request = path.parent / "req-dead.json"
    old_request.write_text("{}", encoding="utf-8")
    old_gate = path.parent / "req-dead.job-ready.json"
    old_gate.write_text("{}", encoding="utf-8")
    old_result = path.parent / "res-dead.json"
    old_result.write_text("{}", encoding="utf-8")
    old_time = _NOW.timestamp() - STALE_FILE_AGE_S - 10

    for old in (old_request, old_gate, old_result):
        os.utime(old, (old_time, old_time))
    prune_stale(tmp_path, 7, now=_NOW)
    assert old_request.exists()
    assert old_gate.exists()
    assert not old_result.exists()
    assert path.parent.is_dir()


def test_failed_envelope_write_leaves_nothing_at_the_destination(tmp_path: Path) -> None:
    """A write failing before the rename never materializes at `path` (D-3)."""
    path = make_request_path(tmp_path, agent_id=7, request_id=UUID(int=1))
    path.mkdir()

    with pytest.raises(OSError):
        write_request(
            path,
            code="x = 1",
            context=_context_description(7),
            timeout_s=1.0,
            state=None,
            incarnation=None,
        )

    assert path.is_dir() and list(path.iterdir()) == []
    assert list(path.parent.glob(".*.tmp")) == []  # the scratch file is cleaned up


def test_envelope_write_commits_the_full_bytes_and_a_second_write_replaces_them(
    tmp_path: Path,
) -> None:
    """The committed file is complete, owner-only, and leaves no scratch behind."""
    path = make_request_path(tmp_path, agent_id=7, request_id=UUID(int=1))
    write_request(
        path,
        code="x = 1",
        context=_context_description(7),
        timeout_s=1.0,
        state=None,
        incarnation=None,
    )
    first = path.read_text(encoding="utf-8")

    write_request(
        path,
        code="x = 2",
        context=_context_description(7),
        timeout_s=2.0,
        state=None,
        incarnation=None,
    )

    assert json.loads(path.read_text(encoding="utf-8"))["code"] == "x = 2"
    assert json.loads(first)["code"] == "x = 1"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(".*.tmp")) == []


def test_orphaned_write_temp_files_are_swept(tmp_path: Path) -> None:
    """A crashed writer's scratch file is swept by a later allocation."""

    path = make_request_path(tmp_path, agent_id=7, request_id=UUID(int=1))
    agent_dir = path.parent
    orphan = agent_dir / ".req-dead.json.deadbeef.tmp"
    orphan.write_text("partial")
    live = agent_dir / ".req-live.json.cafef00d.tmp"
    live.write_text("partial")
    old_time = _NOW.timestamp() - STALE_FILE_AGE_S - 10
    os.utime(orphan, (old_time, old_time))
    os.utime(live, (_NOW.timestamp(), _NOW.timestamp()))

    prune_stale(tmp_path, 7, now=_NOW)

    assert not orphan.exists()
    assert live.exists()


def test_size_ceiling_enforced(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=7, request_id=UUID(int=1))
    write_request(
        path,
        code="x = 1",
        context=_context_description(7),
        timeout_s=1.0,
        state=None,
        incarnation=None,
    )
    path.write_bytes(b"x" * (MAX_ENVELOPE_BYTES + 1))
    with pytest.raises(ValueError, match=r"ceiling.*compact the conversation"):
        read_request(path)


def test_no_agent_dirname(tmp_path: Path) -> None:
    path = make_request_path(tmp_path, agent_id=None, request_id=UUID(int=1))
    assert path.parent.name == "_no_agent_"
