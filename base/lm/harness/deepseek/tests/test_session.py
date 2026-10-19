from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.lm.harness import ClusterFact, EntryPoint, FileSessionRegistry, Handoff, SessionRecord
from base.lm.harness.acp.transport import PermissionOption, PermissionRequest
from base.lm.harness.deepseek import (
    DEFAULT_MODEL,
    InboundQueueFullError,
    SessionClosedError,
)
from base.lm.harness.deepseek.tests.support import AGENT_PID, start
from base.lm.harness.deepseek.usage import session_log_dir
from base.lm.harness.events import (
    AssistantText,
    HandoffInjected,
    SessionBound,
    SessionClosed,
    SessionFaulted,
    SessionStarted,
    TextDelta,
    TurnCompleted,
)
from base.lm.pricing import PriceBook, load_archive


def permission_request(
    kind_options: tuple[str, ...] = ("allow_once", "reject_once"),
) -> PermissionRequest:
    return PermissionRequest(
        session_id="session-1",
        tool_call={"toolCallId": "call-1", "title": "write file"},
        options=tuple(
            PermissionOption(option_id=kind, name=kind, kind=kind) for kind in kind_options
        ),
    )


def usage_log(tmp_path: Path, session_id: str, *, turn: int, text: int, out: int) -> None:
    directory = session_log_dir(tmp_path / ".dsh", tmp_path) / session_id
    directory.mkdir(parents=True, exist_ok=True)
    event = {
        "type": "assistant/message",
        "data": {
            "turn": turn,
            "step": 1,
            "usage": {
                "inputTokens": text,
                "outputTokens": out,
                "cacheReadTokens": 0,
                "cacheWriteTokens": 0,
            },
        },
    }
    (directory / "session.v4.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")


async def test_start_initializes_creates_a_session_and_selects_the_model(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        assert [name for name, _ in harness.connection.requests][:3] == [
            "initialize",
            "session/new",
            "session/set_config_option",
        ]
        assert harness.requests("session/new")[0]["cwd"] == str(tmp_path)
        assert harness.requests("session/set_config_option")[0] == {
            "sessionId": "session-1",
            "configId": "model",
            "value": DEFAULT_MODEL,
        }
        started = await harness.wait_for(lambda event: isinstance(event, SessionStarted))
        assert started.session_id == "session-1"
        assert harness.session.session_id == "session-1"
        assert harness.session.agent_pid == AGENT_PID
    finally:
        await harness.stop()


async def test_a_reasoning_effort_is_selected_after_the_model(tmp_path: Path) -> None:
    harness = await start(tmp_path, reasoning_effort="high")
    try:
        selections = harness.requests("session/set_config_option")
        assert [selection["configId"] for selection in selections] == ["model", "reasoning_effort"]
        assert selections[1]["value"] == "high"
    finally:
        await harness.stop()


async def test_no_model_means_no_selection_request(tmp_path: Path) -> None:
    harness = await start(tmp_path, model=None)
    try:
        assert harness.requests("session/set_config_option") == []
    finally:
        await harness.stop()


async def test_resume_from_the_registry_uses_session_resume(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    registry.save(
        SessionRecord(
            agent_id=41,
            session_id="session-1",
            cwd=str(tmp_path),
            model=DEFAULT_MODEL,
            created_at="2026-10-19T00:00:00+00:00",
            updated_at="2026-10-19T00:00:00+00:00",
        )
    )
    harness = await start(tmp_path, agent_id=41, registry=registry)
    try:
        assert harness.requests("session/new") == []
        assert harness.requests("session/resume")[0]["sessionId"] == "session-1"
        assert harness.session.resumed is True
        bound = await harness.wait_for(lambda event: isinstance(event, SessionBound))
        assert bound.resumed is True
        record = registry.load(41)
        assert record is not None and record.resumes == 1
        assert record.created_at == "2026-10-19T00:00:00+00:00"
    finally:
        await harness.stop()


async def test_a_turn_reports_deltas_the_answer_and_a_completion(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        outcome = await harness.session.submit("hello").outcome()
        assert outcome.text == "echo:hello"
        assert outcome.subtype == "success"
        assert outcome.is_error is False
        assert outcome.interrupted is False
        assert outcome.per_turn_usage is False
        [delta] = harness.of(TextDelta)
        assert delta.text == "echo:hello"
        [answer] = harness.of(AssistantText)
        assert answer.text == "echo:hello"
        [completed] = harness.of(TurnCompleted)
        assert completed.turn_id == outcome.turn_id
        assert completed.num_turns == 1
        assert harness.session.totals.turns == 1
    finally:
        await harness.stop()


async def test_per_turn_usage_comes_from_the_session_log(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        usage_log(tmp_path, "session-1", turn=1, text=120, out=30)
        outcome = await harness.session.submit("hello").outcome()
        assert outcome.per_turn_usage is True
        assert outcome.usage.input_tokens == 120
        assert outcome.usage.output_tokens == 30
        assert harness.session.usage is not None
        assert harness.session.totals.turns == 1
        [completed] = harness.of(TurnCompleted)
        assert completed.usage.input_tokens == 120
    finally:
        await harness.stop()


async def test_a_priced_session_reports_turn_and_cumulative_cost(tmp_path: Path) -> None:
    prices = PriceBook(load_archive(), {})
    harness = await start(tmp_path, prices=prices)
    try:
        usage_log(tmp_path, "session-1", turn=1, text=1000, out=500)
        first = await harness.session.submit("hello").outcome()
        assert first.cost_usd is not None and first.cost_usd > 0
        [completed] = harness.of(TurnCompleted)
        assert completed.cost_usd == first.cost_usd
        assert completed.cumulative_cost_usd == first.cost_usd
        usage_log(tmp_path, "session-1", turn=2, text=2000, out=1000)
        second = await harness.session.submit("again").outcome()
        assert second.cost_usd is not None and second.cost_usd > first.cost_usd
        assert harness.session.totals.cost_usd == first.cost_usd + second.cost_usd
        assert harness.session.totals.cost_known is True
    finally:
        await harness.stop()


async def test_without_a_price_book_the_cost_is_unknown(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        usage_log(tmp_path, "session-1", turn=1, text=1000, out=500)
        outcome = await harness.session.submit("hello").outcome()
        assert outcome.per_turn_usage is True
        assert outcome.cost_usd is None
        [completed] = harness.of(TurnCompleted)
        assert completed.cumulative_cost_usd is None
        assert harness.session.totals.cost_known is False
    finally:
        await harness.stop()


async def test_context_occupancy_follows_the_usage_update(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        await harness.session.submit("hello").outcome()
        assert harness.session.context_occupancy == (12, 1000)
    finally:
        await harness.stop()


async def test_a_repeated_key_returns_the_settled_turn(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        first = harness.session.submit("hello", key="k")
        await first.outcome()
        again = harness.session.submit("hello", key="k")
        assert again.deduplicated is True
        assert again.turn_id == first.turn_id
        assert (await again.outcome()).text == "echo:hello"
        assert len(harness.requests("session/prompt")) == 1
    finally:
        await harness.stop()


async def test_an_empty_message_is_refused(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        with pytest.raises(ValueError):
            harness.session.submit("   ")
    finally:
        await harness.stop()


async def test_a_full_queue_is_refused_instead_of_buffered(tmp_path: Path) -> None:
    harness = await start(tmp_path, max_pending_turns=1)
    try:
        harness.session.submit("slow one")
        await harness.prompt_in_flight()
        harness.session.submit("queued")
        with pytest.raises(InboundQueueFullError) as caught:
            harness.session.submit("one too many")
        assert "limit 1" in str(caught.value)
        assert len(harness.session.pending_turns) == 1
    finally:
        await harness.stop()


async def test_a_queued_turn_runs_after_the_active_one_settles(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        first = harness.session.submit("slow one")
        await harness.prompt_in_flight()
        second = harness.session.submit("second")
        assert harness.session.active_turn_id == first.turn_id
        assert harness.session.pending_turns == (second.turn_id,)
        report = await harness.session.interrupt()
        assert report.settled is True
        assert (await second.outcome()).text == "echo:second"
        assert (await first.outcome()).interrupted is True
    finally:
        await harness.stop()


async def test_interrupt_cancels_the_turn_and_reaps_what_it_spawned(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        ticket = harness.session.submit("slow one")
        await harness.prompt_in_flight()
        harness.probe.spawn(5001, "sleep 1000")
        report = await harness.session.interrupt()
        assert report.turn_id == ticket.turn_id
        assert report.settled is True
        assert report.remaining == ()
        assert [observation.pid for observation in report.observed] == [5001]
        assert harness.probe.killed == [5001]
        assert harness.notifications("session/cancel") == [{"sessionId": "session-1"}]
        outcome = await ticket.outcome()
        assert outcome.interrupted is True
        assert outcome.stop_reason == "cancelled"
    finally:
        await harness.stop()


async def test_interrupt_without_children_reports_a_clean_settle(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        harness.session.submit("slow one")
        await harness.prompt_in_flight()
        report = await harness.session.interrupt()
        assert report.settled is True
        assert report.observed == ()
        assert report.agent_alive is False
    finally:
        await harness.stop()


async def test_a_child_started_before_the_turn_is_not_killed(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        harness.probe.spawn(4001, "long-lived helper")
        harness.session.submit("slow one")
        await harness.prompt_in_flight()
        report = await harness.session.interrupt()
        assert report.observed == ()
        assert harness.probe.killed == []
    finally:
        await harness.stop()


async def test_an_overrunning_turn_is_interrupted(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        ticket = harness.session.submit("timeout please")
        faulted = await harness.wait_for(
            lambda event: isinstance(event, SessionFaulted) and "interrupting" in event.error
        )
        assert faulted.turn_id == ticket.turn_id
        with pytest.raises(SessionClosedError):
            await ticket.outcome()
        assert harness.notifications("session/cancel") == [{"sessionId": "session-1"}]
    finally:
        await harness.stop()


async def test_permission_allow_answers_with_allow_once(tmp_path: Path) -> None:
    harness = await start(tmp_path, permission_mode="allow")
    try:
        assert await harness.session.permission_decision(permission_request()) == "allow_once"
    finally:
        await harness.stop()


async def test_permission_ask_refuses_and_records_the_tool(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        assert await harness.session.permission_decision(permission_request()) == "reject_once"
        await harness.session.submit("hello").outcome()
        [completed] = harness.of(TurnCompleted)
        assert completed.permission_denials == ("write file",)
    finally:
        await harness.stop()


async def test_permission_without_a_reject_option_answers_with_nothing(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        assert (
            await harness.session.permission_decision(permission_request(("allow_once",))) is None
        )
    finally:
        await harness.stop()


async def test_the_handoff_is_the_first_turn_and_is_deduplicated(tmp_path: Path) -> None:
    handoff = Handoff(
        agent_id=41,
        instruction="Take over the dsh adapter tests.",
        machine="ubuntu",
        workspace=tmp_path,
        label="dsh",
        cluster=(ClusterFact(machine="ubuntu", role="worker"),),
        entries=(EntryPoint(name="meiva.tasks", purpose="read the task"),),
    )
    harness = await start(tmp_path, handoff=handoff, agent_id=41)
    try:
        injected = await harness.wait_for(lambda event: isinstance(event, HandoffInjected))
        assert injected.agent_id == 41
        assert injected.size_bytes == handoff.size_bytes()
        assert harness.session.handoff_ticket is not None
        outcome = await harness.session.handoff_ticket.outcome()
        assert handoff.instruction in outcome.text
    finally:
        await harness.stop()


async def test_a_resumed_session_does_not_inject_the_handoff_again(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    registry.save(
        SessionRecord(
            agent_id=41,
            session_id="session-1",
            cwd=str(tmp_path),
            model=DEFAULT_MODEL,
            created_at="2026-10-19T00:00:00+00:00",
            updated_at="2026-10-19T00:00:00+00:00",
        )
    )
    handoff = Handoff(agent_id=41, instruction="go", machine="ubuntu", workspace=tmp_path)
    harness = await start(tmp_path, agent_id=41, registry=registry, handoff=handoff)
    try:
        await harness.session.submit("hello").outcome()
        assert harness.of(HandoffInjected) == []
        assert harness.session.handoff_ticket is None
    finally:
        await harness.stop()


async def test_close_emits_session_closed_and_fails_the_queue(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        harness.session.submit("slow one")
        await harness.prompt_in_flight()
        queued = harness.session.submit("queued")
        await harness.session.close(reason="test done")
        await harness.settle()
        with pytest.raises(SessionClosedError):
            await queued.outcome()
        assert harness.of(SessionClosed)
        assert harness.connection.closed is True
        with pytest.raises(SessionClosedError):
            harness.session.submit("after close")
        await harness.session.close(reason="again")
    finally:
        await harness.stop()


async def test_close_reports_descendants_that_survived(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        harness.probe.spawn(5001, "stubborn", stubborn=True)
        await harness.session.close(reason="test done")
        await harness.settle()
        assert [observation.pid for observation in harness.session.residue] == [5001]
        assert [event for event in harness.of(SessionFaulted) if "survived" in event.error]
    finally:
        await harness.stop()


async def test_a_session_cannot_be_started_twice(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            await harness.session.start()
    finally:
        await harness.stop()


async def test_the_binary_and_profile_come_from_the_config(tmp_path: Path) -> None:
    harness = await start(tmp_path, binary="/usr/bin/dsh", profile="acp")
    try:
        session = harness.session
        connection = session.open_connection()
        assert connection.argv == ("/usr/bin/dsh", "--profile", "acp")
        assert connection.cwd == str(tmp_path)
        assert connection.environ["DSH_HOME"] == str(tmp_path / ".dsh")
    finally:
        await harness.stop()
