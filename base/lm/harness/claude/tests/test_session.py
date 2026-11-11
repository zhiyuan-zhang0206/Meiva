from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from base.lm.harness import FileSessionRegistry, SessionRecord
from base.lm.harness.claude.config import DEFAULT_MODEL
from base.lm.harness.claude.session import SessionClosedError
from base.lm.harness.claude.tests import fake_sdk as fake
from base.lm.harness.claude.tests.fake_sdk import Emit, Say
from base.lm.harness.claude.tests.support import start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import (
    RateLimitObserved,
    SessionBound,
    SessionClosed,
    SessionStarted,
    TurnCompleted,
)


async def test_options_carry_the_boundary_invariants(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        options = harness.client.options
        assert options.model == DEFAULT_MODEL == "claude-sonnet-5-5"
        assert options.cwd == str(tmp_path)
        assert options.setting_sources is None
        assert options.strict_mcp_config is False
        assert options.include_partial_messages is True
        assert options.tools is None
        assert options.env.get("ANTHROPIC_API_KEY") == ""
        assert options.permission_mode == "bypassPermissions"
    finally:
        await harness.stop()


async def test_resume_passes_the_session_id_and_reports_it(tmp_path: Path) -> None:
    harness = await start(tmp_path, resume="sess-42")
    try:
        assert harness.client.options.resume == "sess-42"
        started = await harness.wait_for(lambda event: isinstance(event, SessionStarted))
        assert started.session_id == "sess-42"
        assert harness.session.session_id == "sess-42"
    finally:
        await harness.stop()


async def test_totals_accumulate_across_turns(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Say("one")], [Say("two")]])
    try:
        first = await (await send(harness, "first")).outcome()
        second = await (await send(harness, "second")).outcome()
        totals = harness.session.totals
        assert totals.turns == 2
        assert totals.input_tokens == 8 + 5476, "the prompt totals its cache reads"
        assert totals.output_tokens == 276
        assert totals.cache_read_input_tokens == 5476
        assert totals.cost_usd == pytest.approx(0.0008)
        completed = harness.of(TurnCompleted)
        assert completed[0].cumulative_cost_usd == pytest.approx(0.0004)
        assert completed[1].cumulative_cost_usd == pytest.approx(0.0008)
        assert first.text == "one" and second.text == "two"
    finally:
        await harness.stop()


async def test_rate_limit_signal_reaches_the_stream(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        scripts=[
            [
                Emit(
                    fake.RateLimitEvent(
                        rate_limit_info=fake.RateLimitInfo(
                            status="allowed_warning",
                            utilization=0.96,
                            resets_at=1795000000,
                            rate_limit_type="seven_day",
                        )
                    )
                ),
                Say("done"),
            ]
        ],
    )
    try:
        await (await send(harness, "hello")).outcome()
        [signal] = harness.of(RateLimitObserved)
        assert signal.status == "allowed_warning"
        assert signal.utilization == 0.96
        assert signal.resets_at == 1795000000
        assert signal.rate_limit_type == "seven_day"
    finally:
        await harness.stop()


async def test_close_emits_session_closed_and_disconnects(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        await harness.session.close(reason="done")
        assert harness.client.disconnected is True
        closed = await harness.wait_for(lambda event: isinstance(event, SessionClosed))
        assert closed.reason == "done"
        with pytest.raises(SessionClosedError):
            await send(harness, "too late")
    finally:
        await harness.stop()


async def test_dropped_deltas_are_counted_when_nothing_drains(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Say("long answer")]], max_buffered_events=0)
    try:
        await harness.detach_consumer()
        await send(harness, "hello")
        async with asyncio.timeout(2):
            while harness.session.dropped_deltas < 1:
                await asyncio.sleep(0.002)
        assert harness.session.dropped_deltas == 1
    finally:
        await harness.stop()


async def test_start_binds_the_agent_id_to_the_session(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    harness = await start(tmp_path, agent_id=6984, registry=registry, session_id="sess-a")
    try:
        bound = await harness.wait_for(lambda event: isinstance(event, SessionBound))
        assert bound.agent_id == 6984
        assert bound.session_id == "sess-a"
        assert bound.resumed is False
        record = registry.load(6984, harness="claude_code")
        assert record is not None
        assert record.session_id == "sess-a"
        assert record.model == DEFAULT_MODEL
        assert record.cwd == str(tmp_path)
        assert record.resumes == 0
        assert harness.session.record == record
    finally:
        await harness.stop()


async def test_second_session_resumes_from_the_registry(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    first = await start(tmp_path, agent_id=7001, registry=registry, session_id="sess-first")
    try:
        await first.wait_for(lambda event: isinstance(event, SessionBound))
    finally:
        await first.stop()

    second = await start(tmp_path, agent_id=7001, registry=registry)
    try:
        assert second.client.options.resume == "sess-first"
        assert second.session.resumed is True
        bound = await second.wait_for(
            lambda event: isinstance(event, SessionBound) and event.resumed
        )
        assert bound.session_id == "sess-first"
        record = registry.load(7001, harness="claude_code")
        assert record is not None
        assert record.resumes == 1
    finally:
        await second.stop()


async def test_a_session_resumes_its_own_record_after_another_harness_used_the_agent(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    first = await start(tmp_path, agent_id=7003, registry=registry, session_id="claude-old")
    try:
        await first.wait_for(lambda event: isinstance(event, SessionBound))
    finally:
        await first.stop()
    registry.save(
        SessionRecord(
            agent_id=7003,
            session_id="codex-thread",
            cwd=str(tmp_path),
            model="",
            created_at="2026-11-01T00:00:00+00:00",
            updated_at="2026-11-02T00:00:00+00:00",
            harness="codex",
            generation=0,
        )
    )

    again = await start(tmp_path, agent_id=7003, registry=registry)
    try:
        assert again.client.options.resume == "claude-old"
    finally:
        await again.stop()
    kept = registry.load(7003, harness="codex")
    assert kept is not None and kept.session_id == "codex-thread"


def test_the_shared_package_does_not_reach_into_an_adapter() -> None:
    # meiva:note
    # `base.lm.harness` is the half every provider shares; importing it must not
    # pull in one adapter. The check is on the module source because an
    # import-time side effect is exactly what this test exists to prevent, and
    # importing in-process cannot prove absence.
    # meiva:link base/lm/harness/docs/harness.md
    import base.lm.harness as shared

    source = Path(shared.__file__).read_text()
    assert "claude" not in source
