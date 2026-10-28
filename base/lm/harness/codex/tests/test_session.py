from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.lm.harness.codex.frames import JsonRpcError
from base.lm.harness.codex.tests.fake_app_server import FakeAppServer
from base.lm.harness.codex.tests.support import handoff, start
from base.lm.harness.events import (
    HandoffInjected,
    SessionBound,
    SessionStarted,
    TurnCompleted,
)
from base.lm.harness.registry import FileSessionRegistry, SessionRecord


@pytest.mark.parametrize("resume", [None, "previous-thread"])
async def test_selected_model_and_effort_reach_every_codex_turn(
    tmp_path: Path, resume: str | None
) -> None:
    harness = await start(
        tmp_path,
        server=FakeAppServer(thread_id=resume or "thread-1"),
        resume=resume,
        model="gpt-6-astra",
        reasoning_effort="ultra",
    )
    try:
        for index in range(2):
            ticket = harness.session.submit(f"turn {index}")
            await harness.turn_started(index + 1)
            params = [
                params for method, params in harness.server.requests if method == "turn/start"
            ][-1]
            assert params["model"] == "gpt-6-astra"
            assert params["effort"] == "ultra"
            harness.server.message("done")
            harness.server.complete()
            await asyncio.wait_for(ticket.outcome(), 2)
    finally:
        await harness.stop()


async def test_start_binds_the_thread_and_handshakes(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    harness = await start(tmp_path, registry=registry, agent_id=3)
    try:
        bound = await harness.wait_for(lambda event: isinstance(event, SessionBound))
        started = harness.of(SessionStarted)[0]
        assert started.session_id == "thread-1"
        assert bound.session_id == "thread-1"
        assert bound.resumed is False
        assert harness.server.started is True
        assert harness.server.methods()[:2] == ["initialize", "thread/start"]
        assert ("initialized", None) in harness.server.notifications
    finally:
        await harness.stop()


async def test_turn_streams_text_and_reports_usage(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        ticket = harness.session.submit("do the thing")
        await harness.turn_started()
        harness.server.delta("PO")
        harness.server.delta("NG")
        harness.server.message("PONG")
        harness.server.usage(input_tokens=100, output_tokens=7, cached_input_tokens=40)
        harness.server.complete()
        outcome = await asyncio.wait_for(ticket.outcome(), 2)
        assert outcome.text == "PONG"
        assert outcome.subtype == "completed"
        assert outcome.is_error is False
        assert outcome.usage.input_tokens == 100
        assert outcome.usage.output_tokens == 7
        assert outcome.usage.cache_read_input_tokens == 40
        assert outcome.cost_usd is None
        assert harness.session.totals.turns == 1
        assert harness.session.totals.input_tokens == 100
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert completed.duration_ms == 12
        assert completed.cumulative_cost_usd is None
    finally:
        await harness.stop()


async def test_completion_text_prefers_the_final_answer(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        ticket = harness.session.submit("explain")
        await harness.turn_started()
        harness.server.message("I will look at the file.", phase="commentary")
        harness.server.message("The file defines one function.", phase="final_answer")
        harness.server.complete()
        outcome = await asyncio.wait_for(ticket.outcome(), 2)
        assert outcome.text == "The file defines one function."
    finally:
        await harness.stop()


async def test_failed_turn_reports_the_server_error(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        ticket = harness.session.submit("do the thing")
        await harness.turn_started()
        harness.server.complete(status="failed", error="usage limit reached")
        outcome = await asyncio.wait_for(ticket.outcome(), 2)
        assert outcome.is_error is True
        assert outcome.subtype == "failed"
        assert outcome.stop_reason == "usage limit reached"
    finally:
        await harness.stop()


async def test_resume_uses_the_registry_and_skips_the_handoff(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    registry.save(
        SessionRecord(
            agent_id=7,
            session_id="thread-old",
            cwd=str(tmp_path),
            model="",
            created_at=now,
            updated_at=now,
        )
    )
    harness = await start(tmp_path, registry=registry, agent_id=7, handoff_doc=handoff())
    try:
        bound = await harness.wait_for(lambda event: isinstance(event, SessionBound))
        assert harness.session.resumed is True
        assert harness.server.params_for("thread/resume")[0]["threadId"] == "thread-old"
        assert "thread/start" not in harness.server.methods()
        assert harness.session.handoff_ticket is None
        assert harness.of(HandoffInjected) == []
        assert bound.resumed is True
        record = registry.load(7)
        assert record is not None
        assert record.session_id == "thread-old"
        assert record.resumes == 1
    finally:
        await harness.stop()


async def test_resume_failure_starts_a_new_thread_and_injects_the_handoff(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    registry.save(
        SessionRecord(
            agent_id=7,
            session_id="thread-gone",
            cwd=str(tmp_path),
            model="",
            created_at=now,
            updated_at=now,
        )
    )
    server = FakeAppServer(resume_fails=True)
    with pytest.raises(JsonRpcError, match="thread not found"):
        await start(tmp_path, server=server, registry=registry, agent_id=7, handoff_doc=handoff())
    assert "thread/resume" in server.methods()
    assert "thread/start" not in server.methods()
    record = registry.load(7)
    assert record is not None
    assert record.session_id == "thread-gone"
    assert record.resumes == 0


async def test_thread_id_is_written_to_the_registry(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    harness = await start(tmp_path, registry=registry, agent_id=11)
    try:
        record = registry.load(11)
        assert record is not None
        assert record.session_id == "thread-1"
        assert record.cwd == str(tmp_path)
    finally:
        await harness.stop()


async def test_stale_resumed_turn_notification_is_replaced_by_authoritative_turn_id(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        ticket = harness.session.submit("heartbeat check-in")
        harness.server.usage(
            input_tokens=999,
            output_tokens=99,
            cached_input_tokens=500,
            turn_id="stale-resumed-turn",
        )
        await harness.turn_started()
        harness.server.delta("All ", turn_id="codex-turn-1")
        harness.server.delta("clear.", turn_id="codex-turn-1")
        harness.server.message("All clear.", turn_id="codex-turn-1")
        harness.server.usage(
            input_tokens=120,
            output_tokens=15,
            cached_input_tokens=80,
            turn_id="codex-turn-1",
        )
        harness.server.complete(turn_id="codex-turn-1")
        outcome = await asyncio.wait_for(ticket.outcome(), 2)
        assert outcome.text == "All clear."
        assert outcome.usage.input_tokens == 120
        assert outcome.usage.output_tokens == 15
        assert outcome.usage.cache_read_input_tokens == 80
    finally:
        await harness.stop()
