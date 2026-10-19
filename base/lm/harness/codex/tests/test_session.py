from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from base.lm.harness.codex.tests.fake_app_server import FakeAppServer
from base.lm.harness.codex.tests.support import handoff, start, turn_text
from base.lm.harness.events import (
    HandoffInjected,
    SessionBound,
    SessionFaulted,
    SessionStarted,
    TurnCompleted,
)
from base.lm.harness.registry import FileSessionRegistry, SessionRecord


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
    harness = await start(
        tmp_path, server=server, registry=registry, agent_id=7, handoff_doc=handoff()
    )
    try:
        fault = await harness.wait_for(lambda event: isinstance(event, SessionFaulted))
        assert "thread-gone" in fault.error
        assert "thread/start" in server.methods()
        injected = await harness.wait_for(lambda event: isinstance(event, HandoffInjected))
        assert injected.agent_id == 7
        assert harness.session.resumed is False
        handoff_turn = harness.session.handoff_ticket
        assert handoff_turn is not None
        await harness.turn_started()
        assert "Reply with one word." in turn_text(server.params_for("turn/start")[0])
    finally:
        await harness.stop()


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
