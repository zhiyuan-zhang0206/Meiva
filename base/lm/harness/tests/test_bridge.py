from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    FileSessionRegistry,
    Handoff,
    HarnessBridge,
    HarnessClosedError,
    HarnessRegistration,
    HarnessTurnFaultedError,
    RegistrationStore,
    ScopedSessionRegistry,
    SessionFaulted,
    SessionRecord,
    SessionStarted,
    TurnCompleted,
    Usage,
    handle_digest,
)
from base.lm.harness.wiring import HANDOFF_INSTRUCTION, bridge_for, open_session


class FakeTicket:
    def __init__(self, turn_id: str) -> None:
        self.turn_id = turn_id


def completion(turn_id: str, text: str, usage: Usage | None = None) -> TurnCompleted:
    return TurnCompleted(
        turn_id=turn_id,
        session_id="session-1",
        text=text,
        subtype="success",
        is_error=False,
        interrupted=False,
        stop_reason="end_turn",
        num_turns=1,
        duration_ms=10,
        usage=usage or Usage(input_tokens=10, output_tokens=5),
        cost_usd=0.5,
        cumulative_cost_usd=1.0,
        permission_denials=(),
    )


# meiva:note
# The adapter contract without a provider: start records what it was handed, submit
# queues that turn's completion in order, and the event stream is the queue. That is
# enough to check the bridge's half — which turn it waits for, what it shows, and
# what it counts.
# meiva:link agent/graph/docs/harness-turn.meiva.okf.md
class FakeSession:
    def __init__(self, *, opening: str | None = None, fault: str | None = None) -> None:
        self.started = 0
        self.closed = False
        self.close_reason: str | None = None
        self.handoff: Handoff | None = None
        self.submitted: list[tuple[str, str | None, str]] = []
        self.session_id = None
        self._queue: list[BridgeEvent] = []
        self._n = 0
        self._opening = opening
        self._fault = fault

    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None:
        self.started += 1
        self.handoff = handoff
        self.session_id = "session-1"
        self._queue.append(
            SessionStarted(session_id="session-1", model="claude-sonnet-5-5", cwd="/w")
        )
        if handoff is not None and self._opening is not None:
            self.submit(handoff.render(), key="handoff")

    def submit(self, text: str, *, key: str | None = None) -> FakeTicket:
        self._n += 1
        turn_id = f"t{self._n}"
        self.submitted.append((text, key, turn_id))
        self._queue.append(AssistantText(turn_id=turn_id, text=f"partial-{turn_id}"))
        if self._n == 1 and self._opening is not None:
            self._queue.append(
                completion(turn_id, self._opening, Usage(input_tokens=100, output_tokens=1))
            )
        elif self._fault is None:
            self._queue.append(completion(turn_id, f"reply-{turn_id}"))
        else:
            self._queue.append(SessionFaulted(turn_id=turn_id, error=self._fault))
        return FakeTicket(turn_id)

    def events(self) -> AsyncIterator[BridgeEvent]:
        async def stream() -> AsyncIterator[BridgeEvent]:
            while self._queue:
                yield self._queue.pop(0)

        return stream()

    async def interrupt(self) -> object:
        return object()

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True
        self.close_reason = reason


def make_bridge(
    session: FakeSession,
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
    )


def handoff(agent_id: int = 6984) -> Handoff:
    return Handoff(
        agent_id=agent_id,
        instruction=HANDOFF_INSTRUCTION,
        machine="macmini",
        workspace=Path("/agents/6984"),
    )


async def test_a_turn_returns_its_own_reply_and_the_opening_turn_as_a_prelude() -> None:
    session = FakeSession(opening="acknowledged")
    bridge = make_bridge(session, handoff=handoff())

    turn = await bridge.run_turn("hello", key="inbound-7")

    assert turn.text == "reply-t2", "the handoff turn's text is not this turn's reply"
    assert turn.usage.input_tokens == 10, "a turn reports its own usage"
    assert [opening.text for opening in turn.prelude] == ["acknowledged"]
    assert turn.prelude[0].usage.input_tokens == 100, "the opening turn keeps its own row"
    assert [submitted[1] for submitted in session.submitted] == ["handoff", "inbound-7"]
    assert bridge.model == "claude-sonnet-5-5"


async def test_a_later_turn_has_no_prelude() -> None:
    bridge = make_bridge(FakeSession(opening="acknowledged"), handoff=handoff())

    await bridge.run_turn("one", key="inbound-1")
    turn = await bridge.run_turn("two", key="inbound-2")

    assert turn.prelude == (), "the opening turn is reported once, on the turn that waited for it"


async def test_a_faulted_turn_retires_its_session_and_the_next_one_starts_fresh() -> None:
    faulted, fresh = FakeSession(fault="cli died"), FakeSession()
    sessions = [faulted, fresh]
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        6984,
        lambda _handle: sessions.pop(0),
        machine="macmini",
        handoff=handoff(),
        registration_ttl_seconds=3600.0,
    )

    with pytest.raises(HarnessTurnFaultedError, match="cli died"):
        await bridge.run_turn("hello", key="inbound-1")

    assert faulted.closed, "a faulted session is dropped, not reused for the next inbound"
    turn = await bridge.run_turn("again", key="inbound-2")

    assert turn.text == "reply-t1", "the rebuilt session runs its own first turn"
    assert fresh.started == 1
    assert fresh.handoff is None, "a resume carries the transcript, so no second handoff"


async def test_the_handoff_is_injected_once_and_the_session_started_once() -> None:
    session = FakeSession(opening="acknowledged")
    bridge = make_bridge(session, handoff=handoff())

    await bridge.run_turn("one", key="inbound-1")
    await bridge.run_turn("two", key="inbound-2")

    assert session.started == 1
    assert len(session.submitted) == 3, "handoff + two turns"
    assert session.handoff is not None


async def test_a_faulted_session_fails_the_turn() -> None:
    bridge = make_bridge(FakeSession(fault="cli died"))

    with pytest.raises(HarnessTurnFaultedError, match="cli died"):
        await bridge.run_turn("hello", key="inbound-1")


async def test_close_closes_the_session_and_refuses_later_turns() -> None:
    session = FakeSession()
    bridge = make_bridge(session)

    await bridge.run_turn("hello", key="inbound-1")
    await bridge.close(reason="evicted")

    assert session.closed
    assert session.close_reason == "evicted"
    with pytest.raises(HarnessClosedError):
        await bridge.run_turn("again", key="inbound-2")


def test_the_built_in_loop_has_no_bridge() -> None:
    assert (
        bridge_for(
            Harness.MEIVA,
            agent_id=1,
            machine="macmini",
            workspace=Path("/agents/6984"),
            registry=FileSessionRegistry(Path("/registry")),
            registration_ttl_seconds=3600.0,
        )
        is None
    )


def test_an_external_harness_builds_its_handoff() -> None:
    bridge = bridge_for(
        Harness.CLAUDE_CODE,
        agent_id=6984,
        machine="macmini",
        workspace=Path("/agents/6984"),
        registry=FileSessionRegistry(Path("/registry")),
        registration_ttl_seconds=3600.0,
    )
    assert bridge is not None
    assert bridge.harness is Harness.CLAUDE_CODE


def test_an_adapter_less_harness_is_refused() -> None:
    with pytest.raises(ValueError, match="no session adapter"):
        open_session(
            Harness.ANTIGRAVITY,
            agent_id=1,
            cwd=Path("/agents/6984"),
            registry=FileSessionRegistry(Path("/registry")),
        )


def record(harness: str = "meiva") -> SessionRecord:
    return SessionRecord(
        agent_id=6984,
        session_id="session-1",
        cwd="/w",
        model="claude-sonnet-5-5",
        created_at="2026-10-19T08:00:00+00:00",
        updated_at="2026-10-19T08:00:00+00:00",
        harness=harness,
    )


class RecordingRegistry(FileSessionRegistry):
    def __init__(self, root: Path, saved: list[SessionRecord]) -> None:
        super().__init__(root)
        self.saved = saved

    def save(self, record: SessionRecord) -> None:
        self.saved.append(record)


def test_the_scope_stamps_every_save() -> None:
    saved: list[SessionRecord] = []
    scoped = ScopedSessionRegistry(
        inner=RecordingRegistry(Path("/nonexistent"), saved), harness=Harness.CODEX
    )

    scoped.save(record(harness="meiva"))

    assert saved[0].harness == "codex", "a save through the scope always names the scope"


def test_the_scope_binds_every_load(tmp_path: Path) -> None:
    inner = FileSessionRegistry(tmp_path / "sessions")
    inner.save(replace(record(), harness="claude_code"))
    scoped = ScopedSessionRegistry(inner=inner, harness=Harness.CLAUDE_CODE)

    assert scoped.load(6984) is not None
    with pytest.raises(ValueError, match="refusing to resume across harnesses"):
        ScopedSessionRegistry(inner=inner, harness=Harness.CODEX).load(6984)


def test_the_scope_refuses_an_explicit_other_harness() -> None:
    scoped = ScopedSessionRegistry(
        inner=FileSessionRegistry(Path("/nonexistent")), harness=Harness.CLAUDE_CODE
    )
    with pytest.raises(ValueError, match="scoped to harness"):
        scoped.load(6984, harness="codex")


def test_a_session_registers_its_handle_digest_and_retires_the_line(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    session = FakeSession()
    handles: list[str] = []
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        6984,
        lambda handle: handles.append(handle) or session,
        machine="macmini",
        registrations=store,
        registration_ttl_seconds=3600.0,
    )

    asyncio.run(bridge.run_turn("hello", key="inbound-1"))

    registration = store.read(6984)
    assert registration is not None
    assert registration.expires_at is not None, "a row without an expiry outlives its host"
    assert registration.external_session_id == "session-1"
    assert registration.machine == "macmini"
    assert registration.handle_sha256 == handle_digest(handles[0]), "only the digest lands on disk"
    assert handles[0] not in registration.handle_sha256

    asyncio.run(bridge.close(reason="evicted"))
    assert store.read(6984) is None


def test_a_registration_expires_within_the_configured_ttl(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    bridge = make_bridge(FakeSession(), registrations=store)

    asyncio.run(bridge.run_turn("hello", key="inbound-1"))

    written = json.loads((tmp_path / "registrations" / "6984.json").read_text(encoding="utf-8"))
    assert written["expires_at"], "a row without an expiry is a capability with no end"
    created = datetime.fromisoformat(written["created_at"])
    expires = datetime.fromisoformat(written["expires_at"])
    assert expires - created == timedelta(seconds=3600)
    assert expires > datetime.now(UTC), "a live controller's row is valid, not expired"


def test_every_turn_renews_the_row_without_restamping_its_creation(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    bridge = make_bridge(FakeSession(), registrations=store)

    asyncio.run(bridge.run_turn("one", key="inbound-1"))
    first = store.read(6984)
    asyncio.run(bridge.run_turn("two", key="inbound-2"))
    second = store.read(6984)

    assert first is not None and second is not None
    assert second.created_at == first.created_at, "a renewal is not a new registration"
    assert second.expires_at >= first.expires_at, "each turn pushes the deadline forward"


def test_the_sweep_drops_a_registration_whose_controller_is_gone(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    store.write(registration(controller_pid=999_999_999))
    store.write(registration(agent_id=6985, controller_pid=os.getpid()))

    assert store.sweep() == [6984]
    assert store.read(6985) is not None


def test_the_sweep_drops_an_expired_registration(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    store.write(registration(expires_at="2000-01-01T00:00:00+00:00"))

    assert store.sweep() == [6984]


def registration(
    agent_id: int = 6984,
    controller_pid: int = 1,
    expires_at: str | None = None,
) -> HarnessRegistration:
    return HarnessRegistration(
        agent_id=agent_id,
        harness="claude_code",
        external_session_id="session-1",
        machine="macmini",
        controller_pid=controller_pid,
        handle_sha256="0" * 64,
        created_at="2026-10-19T08:00:00+00:00",
        expires_at=expires_at or "2027-10-19T08:00:00+00:00",
        status="attached",
    )
