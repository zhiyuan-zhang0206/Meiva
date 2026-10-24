# meiva:note
# How the relay lives beside its heartbeat, and how it words a lease's end. Both are plain
# coroutines and plain prints: nothing is replaced, the cases write down what each side does.

from __future__ import annotations

import asyncio
import io
from dataclasses import replace

import pytest

from base.agents.impersonation.status import ImpersonationStatus
from cli.commands.agents.cleanup_after import run_then_clean_up
from cli.commands.agents.impersonation_adapters import ClaudeStdout, CodexSteer
from cli.commands.agents.impersonation_relay import announce_end, run_beside_heartbeat
from cli.commands.agents.relay.inbox import EndedLease, LeaseRef
from cli.commands.agents.tests.relay.relay_inputs import LEASE_UUID, snapshot


async def _beat(
    events: list[str], *, error: Exception | None, outlives_the_relay: bool
) -> Exception | None:
    events.append("heartbeat_started")
    if outlives_the_relay:
        try:
            await asyncio.Event().wait()
        finally:
            events.append("heartbeat_joined")
    return error


async def _relay(events: list[str], *, interrupted: bool) -> None:
    for _ in range(4):
        await asyncio.sleep(0)
    assert events == ["heartbeat_started"]
    events.append("relay_finished")
    if interrupted:
        raise KeyboardInterrupt


@pytest.mark.parametrize("outlives_the_relay", [False, True])
def test_a_clean_relay_with_a_healthy_heartbeat_ends_without_error(
    outlives_the_relay: bool,
) -> None:
    events: list[str] = []
    asyncio.run(
        run_beside_heartbeat(
            _beat(events, error=None, outlives_the_relay=outlives_the_relay),
            _relay(events, interrupted=False),
        )
    )
    assert events == ["heartbeat_started", "relay_finished"] + (
        ["heartbeat_joined"] if outlives_the_relay else []
    )


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError, OSError, TypeError])
def test_heartbeat_failure_does_not_cancel_inbox_or_change_cli_error_contract(
    error_type: type[Exception],
) -> None:
    events: list[str] = []
    error = error_type("heartbeat defect")
    with pytest.raises(error_type) as caught:
        asyncio.run(
            run_beside_heartbeat(
                _beat(events, error=error, outlives_the_relay=False),
                _relay(events, interrupted=False),
            )
        )
    assert caught.value is error
    assert events == ["heartbeat_started", "relay_finished"]


def test_an_interrupt_stops_the_relay_and_joins_the_heartbeat_first() -> None:
    events: list[str] = []
    with pytest.raises(KeyboardInterrupt):
        asyncio.run(
            run_beside_heartbeat(
                _beat(events, error=None, outlives_the_relay=True),
                _relay(events, interrupted=True),
            )
        )
    assert events == ["heartbeat_started", "relay_finished", "heartbeat_joined"]


REF = LeaseRef(agent_id=42, lease_ref=3)


def test_stdio_notice_uses_immutable_historical_snapshot() -> None:
    stream = io.StringIO()
    ended_at = "2026-10-05T10:00:00+00:00"
    record = EndedLease(
        session_id=0,
        lease_id=str(LEASE_UUID),
        agent_id=42,
        status="expired",
        reason="terminated: agent was terminated",
        ended_at=ended_at,
    )
    page = snapshot(status=ImpersonationStatus.EXPIRED, ended=record)
    assert announce_end(page, LeaseRef(999, 1), ClaudeStdout(stream)) is True
    out = stream.getvalue()
    assert f"lease 0 ({LEASE_UUID}) for agent 42" in out
    assert ended_at in out
    assert f"Notice ID: impersonation-ended:{LEASE_UUID}" in out
    assert "does not end or cancel any newer" in out
    assert "no active native runtime is implied" in out


def test_a_lease_without_a_terminal_record_keeps_the_supplied_scope_and_says_so() -> None:
    stream = io.StringIO()
    page = snapshot(status=ImpersonationStatus.REJECTED, end_reason="relay would not start")
    assert announce_end(page, REF, ClaudeStdout(stream)) is True
    out = stream.getvalue()
    assert "lease 3 (3) for agent 42 ended: rejected." in out
    assert "Reason: relay would not start." in out
    assert "unknown (legacy record)" in out


def test_a_release_ends_the_relay_without_a_notice() -> None:
    stream = io.StringIO()
    page = snapshot(status=ImpersonationStatus.RELEASED)
    assert announce_end(page, REF, ClaudeStdout(stream)) is True
    assert stream.getvalue() == ""


def test_a_live_lease_is_not_ended() -> None:
    stream = io.StringIO()
    assert announce_end(snapshot(), REF, ClaudeStdout(stream)) is False
    assert stream.getvalue() == ""


def test_codex_relay_never_duplicates_the_host_terminal_notice() -> None:
    page = replace(snapshot(), status=ImpersonationStatus.EXPIRED)
    adapter = CodexSteer(LEASE_UUID, "unix:///nowhere.sock")
    assert announce_end(page, REF, adapter) is True


def test_a_transport_that_is_gone_fails_the_notice_instead_of_losing_it_silently() -> None:
    stream = io.StringIO()
    stream.close()
    page = snapshot(status=ImpersonationStatus.EXPIRED)
    with pytest.raises(ValueError, match="closed file"):
        announce_end(page, REF, ClaudeStdout(stream))


async def _body_that_fails_with(primary: BaseException) -> None:
    raise primary


async def _clean_up(events: list[str], *, error: Exception | None) -> None:
    events.append("clean_up")
    if error is not None:
        raise error


@pytest.mark.parametrize("primary", [RuntimeError("host failure"), asyncio.CancelledError()])
def test_listener_stop_failure_preserves_relay_primary(
    primary: BaseException,
) -> None:
    events: list[str] = []
    cleanup = ValueError("listener cleanup invariant")
    with pytest.raises(type(primary)) as caught:
        asyncio.run(
            run_then_clean_up(
                _body_that_fails_with(primary),
                _clean_up(events, error=cleanup),
                what="Redis listener stop",
            )
        )
    assert caught.value is primary
    assert any("listener cleanup invariant" in note for note in primary.__notes__)
    assert events == ["clean_up"]


def test_listener_stop_failure_fails_otherwise_successful_relay() -> None:
    events: list[str] = []
    cleanup = ValueError("listener cleanup invariant")

    async def body() -> None:
        events.append("body")

    with pytest.raises(ValueError) as caught:
        asyncio.run(
            run_then_clean_up(body(), _clean_up(events, error=cleanup), what="Redis listener stop")
        )
    assert caught.value is cleanup
    assert events == ["body", "clean_up"]


def test_the_relay_result_is_returned_after_a_clean_cleanup() -> None:
    events: list[str] = []

    async def body() -> str:
        return "served"

    assert (
        asyncio.run(
            run_then_clean_up(body(), _clean_up(events, error=None), what="Redis listener stop")
        )
        == "served"
    )
    assert events == ["clean_up"]
