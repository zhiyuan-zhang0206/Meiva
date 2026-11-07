from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncIterator
from pathlib import Path

from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.lm.harness import (
    BridgeEvent,
    ForegroundSteering,
    Handoff,
    HarnessBridge,
    HarnessSession,
    TurnCompleted,
    Usage,
)
from base.lm.harness.base_session import SessionBinding
from base.lm.harness.claude import BridgeConfig
from base.lm.harness.claude.session import ClaudeSession
from base.lm.harness.claude.tests.fake_sdk import Block, FakeSDK, ScriptedProbe
from base.lm.harness.registration import RegistrationStore, SecureRandom


class FakeTicket:
    deduplicated: bool = False
    settled: bool = False

    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id

    async def outcome(self) -> TurnCompleted:
        return TurnCompleted(
            turn_id=self.turn_id,
            session_id="session-1",
            text="",
            subtype="success",
            is_error=False,
            interrupted=False,
            stop_reason="end_turn",
            num_turns=1,
            duration_ms=10,
            usage=Usage(input_tokens=10, output_tokens=5),
            cost_usd=0.5,
            cumulative_cost_usd=1.0,
            permission_denials=(),
        )


class FakeSession:
    def __init__(self) -> None:
        self.started = 0
        self.closed = False
        self.handoff: Handoff | None = None
        self.handoff_ticket: FakeTicket | None = None
        self.submitted: list[tuple[str, str | None, str]] = []
        self.session_id: str | None = None
        self.resumed = False
        self.exit_code: int | None = None
        self._n = 0

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        self.started += 1
        self.handoff = handoff
        self.session_id = "session-1"

    def submit(self, text: str, *, key: str | None = None) -> FakeTicket:
        self._n += 1
        turn_id = f"t{self._n}"
        self.submitted.append((text, key, turn_id))
        return FakeTicket(turn_id)

    def events(self) -> AsyncIterator[BridgeEvent]:
        async def stream() -> AsyncIterator[BridgeEvent]:
            for item in ():
                yield item

        return stream()

    async def interrupt(self, *, turn_id: str | None = None) -> object:
        return object()

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True


class FakeSteerableSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.steering_available = False
        self.steered: list[tuple[str, str]] = []

    async def steer(self, text: str, *, key: str) -> bool:
        if not self.steering_available:
            return False
        self.steered.append((text, key))
        return True


def make_bridge(
    session: HarnessSession,
    *,
    handoff: Handoff | None = None,
    registrations: RegistrationStore | None = None,
) -> HarnessBridge:
    return HarnessBridge(
        Harness.CLAUDE_CODE,
        6984,
        lambda _handle: session,
        machine="macmini",
        handoff=handoff,
        registrations=registrations,
        registration_ttl_seconds=3600.0,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )


async def test_bridge_steering_delegates_to_steerable_session() -> None:
    session = FakeSteerableSession()
    bridge = make_bridge(session)
    assert isinstance(bridge, ForegroundSteering)
    assert not bridge.steering_available
    assert not await bridge.steer("change direction", key="inbound-1")

    _ = await bridge.ensure()
    assert not bridge.steering_available
    session.steering_available = True
    assert bridge.steering_available

    accepted = await bridge.steer("change direction", key="inbound-1")
    assert accepted is True
    assert session.steered == [("change direction", "inbound-1")]

    session.steering_available = False
    assert not bridge.steering_available
    assert not await bridge.steer("again", key="inbound-2")


async def test_bridge_steering_returns_false_for_non_steerable_session() -> None:
    session = FakeSession()
    bridge = make_bridge(session)
    _ = await bridge.ensure()
    assert not bridge.steering_available
    assert not await bridge.steer("change direction", key="inbound-1")


async def test_bridge_steering_with_claude_session(tmp_path: Path) -> None:
    sdk = FakeSDK([[Block()]], cli_pid=4242)
    probe = ScriptedProbe()
    probe.alive.add(4242)
    sdk.probe = probe
    claude = ClaudeSession(
        BridgeConfig(cwd=tmp_path),
        environ={},
        binding=SessionBinding(),
        sdk=sdk,
        probe=probe,
        clock=system_clock(),
    )
    bridge = make_bridge(claude)
    assert not bridge.steering_available
    assert not await bridge.steer("early", key="inbound-0")

    _ = await bridge.ensure()
    _ = claude.submit("first")
    await asyncio.sleep(0.01)

    assert bridge.steering_available
    assert await bridge.steer("steer text", key="inbound-1")
    assert sdk.clients[0].queries == ["first", "steer text"]

    await bridge.close()
    assert not bridge.steering_available
    assert not await bridge.steer("late", key="inbound-2")
