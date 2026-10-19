from __future__ import annotations

from pathlib import Path

import pytest

from base.lm.claude_sdk import (
    DEFAULT_MODEL,
    ClusterFact,
    EntryPoint,
    FileSessionRegistry,
    Handoff,
    SessionClosedError,
)
from base.lm.claude_sdk.events import (
    HandoffInjected,
    RateLimitObserved,
    SessionBound,
    SessionClosed,
    SessionFaulted,
    SessionStarted,
    TurnCompleted,
)
from base.lm.claude_sdk.tests import fake_sdk as fake
from base.lm.claude_sdk.tests.fake_sdk import Block, Emit, Say
from base.lm.claude_sdk.tests.support import start


async def test_options_carry_the_boundary_invariants(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        options = harness.client.options
        assert options.model == DEFAULT_MODEL == "claude-sonnet-5-5"
        assert options.cwd == str(tmp_path)
        assert options.setting_sources == []
        assert options.strict_mcp_config is True
        assert options.include_partial_messages is True
        assert "Bash" in list(options.tools or [])
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
        first = await harness.session.submit("first").outcome()
        second = await harness.session.submit("second").outcome()
        totals = harness.session.totals
        assert totals.turns == 2
        assert totals.input_tokens == 8
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
        await harness.session.submit("hello").outcome()
        [signal] = harness.of(RateLimitObserved)
        assert signal.status == "allowed_warning"
        assert signal.utilization == 0.96
        assert signal.resets_at == 1795000000
        assert signal.rate_limit_type == "seven_day"
    finally:
        await harness.stop()


async def test_turn_deadline_interrupts_a_wedged_turn(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Block()]], turn_timeout_seconds=0.05)
    try:
        ticket = harness.session.submit("wedge")
        faulted = await harness.wait_for(
            lambda event: isinstance(event, SessionFaulted) and "interrupting" in event.error
        )
        assert faulted.turn_id == ticket.turn_id
        assert harness.client.interrupts == 1
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
            harness.session.submit("too late")
    finally:
        await harness.stop()


async def test_dropped_deltas_are_counted_when_nothing_drains(tmp_path: Path) -> None:
    harness = await start(tmp_path, scripts=[[Say("long answer")]], max_buffered_events=0)
    try:
        await harness.detach_consumer()
        outcome = await harness.session.submit("hello").outcome()
        assert outcome.text == "long answer"
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
        record = registry.load(6984)
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
        record = registry.load(7001)
        assert record is not None
        assert record.resumes == 1
    finally:
        await second.stop()


async def test_handoff_is_injected_as_the_first_turn(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    handoff = Handoff(
        agent_id=6984,
        instruction="Read hello.txt and reply with the third word.",
        machine="macmini",
        workspace=tmp_path,
        label="claude-sdk-bridge",
        cluster=(ClusterFact("macmini", "primary host"),),
        entries=(EntryPoint("meiva.files", "read and write files"),),
    )
    harness = await start(
        tmp_path,
        agent_id=6984,
        registry=registry,
        handoff=handoff,
        scripts=[[Say("ack")], [Say("brown")]],
    )
    try:
        injected = await harness.wait_for(lambda event: isinstance(event, HandoffInjected))
        assert injected.agent_id == 6984
        assert injected.size_bytes == handoff.size_bytes()
        outcome = await harness.session.submit("Read hello.txt.").outcome()
        assert outcome.text == "brown"
        assert harness.client.queries == [handoff.render().strip(), "Read hello.txt."]
    finally:
        await harness.stop()


async def test_resume_does_not_reinject_the_handoff(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    handoff = Handoff(agent_id=7002, instruction="Say OK.", machine="macmini", workspace=tmp_path)
    first = await start(
        tmp_path, agent_id=7002, registry=registry, handoff=handoff, scripts=[[Say("OK")]]
    )
    try:
        await first.wait_for(lambda event: isinstance(event, SessionBound))
        await first.session.submit("go").outcome()
    finally:
        await first.stop()

    second = await start(
        tmp_path, agent_id=7002, registry=registry, handoff=handoff, scripts=[[Say("still here")]]
    )
    try:
        assert second.session.resumed is True
        assert second.client.queries == []
        assert (await second.session.submit("go").outcome()).text == "still here"
        assert second.of(HandoffInjected) == []
    finally:
        await second.stop()
