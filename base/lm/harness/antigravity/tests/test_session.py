from __future__ import annotations

import asyncio
import json
from pathlib import Path

from base.lm.harness.antigravity.tests.support import running, say, start
from base.lm.harness.antigravity.timing_store import timing_path
from base.lm.harness.antigravity.transcript.file import AntigravityRecordFile
from base.lm.harness.antigravity.transcript.window import EntryParser
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import (
    AssistantText,
    SessionBound,
    SessionClosed,
    SessionStarted,
    TextDelta,
    TurnCompleted,
)
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.session.transcript.entries import Reasoning
from base.lm.harness.session.transcript.record_file import UnreadRows
from base.log.tests.log_records import LogRecords, loguru_records

__all__ = ["loguru_records"]


async def test_a_turn_streams_text_and_completes(tmp_path: Path) -> None:
    harness = await start(
        tmp_path, turns=[say("hello")], agent_id=7, registry=FileSessionRegistry(tmp_path / "reg")
    )
    try:
        ticket = await send(harness, "hi")
        outcome = await ticket.outcome()
        assert outcome.text == "hello"
        assert outcome.is_error is False
        assert outcome.subtype == "success"
        started = harness.of(SessionStarted)[0]
        assert started.session_id == "conv-fake"
        assert harness.of(SessionBound)[0].agent_id == 7
        assert harness.of(TextDelta)[0].text == "hello"
        assert harness.of(AssistantText)[0].text == "hello"
        completed = harness.of(TurnCompleted)[0]
        assert completed.turn_id == ticket.turn_id
        assert completed.num_turns == 1
        record = harness.session.record
        assert record is not None and record.session_id == "conv-fake"
    finally:
        await harness.stop()


async def test_close_ends_the_event_stream(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("bye")])
    await harness.stop()
    assert harness.of(SessionClosed)


async def test_two_turns_reuse_one_process(tmp_path: Path) -> None:
    harness = await start(tmp_path, turns=[say("one"), say("two")])
    try:
        first = await (await send(harness, "a")).outcome()
        second = await (await send(harness, "b")).outcome()
        assert (first.text, second.text) == ("one", "two")
        assert len(harness.argv_seen()) > 0
        assert "--conversation" not in harness.argv_seen()
    finally:
        await harness.stop()


async def test_turn_failure_populates_errors(tmp_path: Path) -> None:
    steps: list[dict[str, object]] = []
    turn_spec: dict[str, object] = {
        "steps": steps,
        "result": {
            "status": "ERROR",
            "error": "API error (attempt 1): 500 Internal Server Error",
            "response": "MEIVA_HIDE_FROM_USER\nhidden output",
        },
    }
    harness = await start(tmp_path, turns=[turn_spec])
    try:
        ticket = await send(harness, "fail please")
        outcome = await ticket.outcome()
        assert outcome.is_error is True
        assert outcome.errors == ("API error (attempt 1): 500 Internal Server Error",)
        completed = harness.of(TurnCompleted)[0]
        assert completed.errors == ("API error (attempt 1): 500 Internal Server Error",)
    finally:
        await harness.stop()


def _write_native_transcript(home: Path, conversation_id: str) -> Path:
    path = (
        home
        / ".gemini"
        / "antigravity-cli"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "step_index": 0,
                "created_at": "2026-11-17T05:30:00Z",
                "thinking": "synthetic thought",
                "content": "hello",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _measured_reasoning(path: Path) -> list[int | None]:
    entries = EntryParser(UnreadRows()).entries(AntigravityRecordFile(path).rows_after(0).rows)
    return [entry.duration_ms for entry in entries if isinstance(entry, Reasoning)]


async def test_step_done_and_turn_end_trigger_timing_collection(tmp_path: Path) -> None:
    home = tmp_path / "home"
    calls = tmp_path / "trajectory-calls.jsonl"
    fixture = Path(__file__).with_name("fixtures") / "native_thinking_timing.json"
    harness = await start(
        tmp_path,
        turns=[say("hello")],
        extra_environ={
            "HOME": str(home),
            "FAKE_AGY_TRAJECTORY_CALLS": str(calls),
            "FAKE_AGY_TRAJECTORY_FIXTURE": str(fixture),
        },
    )
    try:
        transcript = _write_native_transcript(home, "conv-fake")
        ticket = await send(harness, "hi")
        outcome = await ticket.outcome()
        assert outcome.text == "hello"
        await harness.wait_until(lambda: timing_path(transcript).is_file())
        assert _measured_reasoning(transcript) == [30265]
        assert len(calls.read_text().splitlines()) >= 1
    finally:
        await harness.stop()


async def test_turn_end_without_agent_response_step_still_collects_timing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    calls = tmp_path / "trajectory-calls.jsonl"
    fixture = Path(__file__).with_name("fixtures") / "native_thinking_timing.json"
    turn_without_step: dict[str, object] = {
        "steps": [],
        "result": {"status": "SUCCESS", "response": "done"},
    }
    harness = await start(
        tmp_path,
        turns=[turn_without_step],
        extra_environ={
            "HOME": str(home),
            "FAKE_AGY_TRAJECTORY_CALLS": str(calls),
            "FAKE_AGY_TRAJECTORY_FIXTURE": str(fixture),
        },
    )
    try:
        transcript = _write_native_transcript(home, "conv-fake")
        ticket = await send(harness, "hi")
        outcome = await ticket.outcome()
        assert outcome.text == "done"
        await harness.wait_until(lambda: timing_path(transcript).is_file())
        assert _measured_reasoning(transcript) == [30265]
        assert len(calls.read_text().splitlines()) >= 1
    finally:
        await harness.stop()


async def test_timing_refresh_requests_are_coalesced_and_throttled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    calls = tmp_path / "trajectory-calls.jsonl"
    fixture = Path(__file__).with_name("fixtures") / "native_thinking_timing.json"
    harness = await start(
        tmp_path,
        turns=[{"hang": True}],
        extra_environ={
            "HOME": str(home),
            "FAKE_AGY_TRAJECTORY_CALLS": str(calls),
            "FAKE_AGY_TRAJECTORY_FIXTURE": str(fixture),
        },
    )
    try:
        transcript = _write_native_transcript(home, "conv-fake")
        _ = await send(harness, "work")
        await running(harness, ["work"])
        for _ in range(5):
            harness.session.request_timing_refresh()
        await harness.wait_until(lambda: timing_path(transcript).is_file())
        await asyncio.sleep(0.15)
        call_count = len(calls.read_text().splitlines())
        assert call_count <= 2, f"burst refresh requests must coalesce, got {call_count} RPCs"
        assert _measured_reasoning(transcript) == [30265]
    finally:
        await harness.stop()


async def test_interrupt_cancels_inflight_timing_refresh_without_warning(
    tmp_path: Path,
    loguru_records: LogRecords,
) -> None:
    home = tmp_path / "home"
    sampled = tmp_path / "trajectory-sampled.jsonl"
    release = tmp_path / "trajectory-release"
    fixture = Path(__file__).with_name("fixtures") / "native_thinking_timing.json"
    harness = await start(
        tmp_path,
        turns=[{"hang": True}],
        extra_environ={
            "HOME": str(home),
            "FAKE_AGY_BLOCK_TRAJECTORY_CALL": "1",
            "FAKE_AGY_TRAJECTORY_SAMPLED": str(sampled),
            "FAKE_AGY_TRAJECTORY_RELEASE": str(release),
            "FAKE_AGY_TRAJECTORY_FIXTURE": str(fixture),
        },
    )
    try:
        _ = _write_native_transcript(home, "conv-fake")
        _ = await send(harness, "work")
        await running(harness, ["work"])
        harness.session.request_timing_refresh()
        await harness.wait_until(sampled.exists)
        task = harness.session._timing_refresh_task
        assert task is not None and not task.done()

        _ = await harness.session.interrupt()
        release.touch()
        await harness.wait_until(task.done)
        assert harness.session._timing_refresh_task is None
        assert harness.session._timing_refresh_pending is False
        assert loguru_records.events("harness_timing_collection_failed") == []
    finally:
        release.touch(exist_ok=True)
        await harness.stop()
