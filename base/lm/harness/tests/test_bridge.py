from __future__ import annotations

import asyncio
import base64
import os
import random
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import clock_at, system_clock
from base.clock.tests.seeded_rng import seeded_rng
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    FileSessionRegistry,
    Handoff,
    HarnessBridge,
    HarnessClosedError,
    HarnessRegistration,
    HarnessSetupError,
    HarnessTurnFaultedError,
    InputInContext,
    RegistrationStore,
    SessionFailed,
    SessionFaulted,
    SessionStarted,
    TurnCompleted,
    UnsolicitedOutput,
    Usage,
    handle_digest,
)
from base.lm.harness.antigravity import AgySession
from base.lm.harness.claude.session import ClaudeSession
from base.lm.harness.codex.session import CodexSession
from base.lm.harness.delivery import HarnessInput, Reconciliation
from base.lm.harness.registration import SecureRandom
from base.lm.harness.tests.bridge_support import run_turn
from base.lm.harness.tests.roots import bridge_root_values
from base.lm.harness.wiring import HANDOFF_INSTRUCTION, bridge_for, open_session


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
# The adapter contract without a provider: start records what it was handed, accept
# queues that turn's completion in order, and the event stream is the queue. That is
# enough to check the bridge's half — which turn it waits for, what it shows, and
# what it counts.
# meiva:link agent/graph/docs/harness-turn.md
class FakeSession:
    def __init__(
        self,
        *,
        opening: str | None = None,
        fault: str | None = None,
        during: Sequence[BridgeEvent] = (),
        resumed: bool = False,
    ) -> None:
        self.started = 0
        self.closed = False
        self.close_reason: str | None = None
        self.handoff: Handoff | None = None
        self.submitted: list[tuple[str, str | None, str]] = []
        self.session_id = None
        self.generation = 1
        self.adopted: list[str] = []
        self.resumed = resumed
        # meiva:note
        # The adapter contract's third state holder (task #5616): a fake has no child
        # process, so it answers None unless a test sets one.
        self.exit_code: int | None = None
        self._queue: list[BridgeEvent] = []
        self._n = 0
        self._opening = opening
        self._fault = fault
        # meiva:note
        # Events a provider emits while the turn runs and before its own result: a
        # message about another turn, a report the provider answered itself.
        # meiva:link agent/graph/docs/harness-turn.md
        self._during = tuple(during)

    async def start(self, *, resume: str | None = None) -> None:
        self.started += 1
        self.session_id = "session-1"
        self._queue.append(
            SessionStarted(session_id="session-1", model="claude-sonnet-5-5", cwd="/w")
        )

    async def deliver(self, inputs: Sequence[HarnessInput]) -> None:
        for item in inputs:
            _ = self.accept(item.text, key=item.key)

    async def reconcile(self, keys: Sequence[str]) -> Mapping[str, Reconciliation]:
        return dict.fromkeys(keys, Reconciliation.NOT_FOUND)

    def adopt(self, keys: Sequence[str]) -> None:
        self.adopted.extend(keys)

    def accept(self, text: str, *, key: str | None = None) -> None:
        self._n += 1
        turn_id = f"t{self._n}"
        self.submitted.append((text, key, turn_id))
        if key is not None:
            self._queue.append(InputInContext(key, turn_id, 1))
        self._queue.append(AssistantText(turn_id=turn_id, text=f"partial-{turn_id}"))
        self._queue.extend(self._during)
        if self._n == 1 and self._opening is not None:
            self._queue.append(
                completion(turn_id, self._opening, Usage(input_tokens=100, output_tokens=1))
            )
        elif self._fault is None:
            self._queue.append(completion(turn_id, f"reply-{turn_id}"))
        else:
            self._queue.append(SessionFaulted(turn_id=turn_id, error=self._fault))

    def events(self) -> AsyncIterator[BridgeEvent]:
        async def stream() -> AsyncIterator[BridgeEvent]:
            while not self.closed:
                if self._queue:
                    yield self._queue.pop(0)
                else:
                    await asyncio.sleep(0.001)

        return stream()

    async def interrupt(self, *, turn_id: str | None = None) -> object:
        return object()

    async def close(self, *, reason: str = "closed") -> None:
        self.closed = True
        self.close_reason = reason


class RefusingSession(FakeSession):
    def accept(self, text: str, *, key: str | None = None) -> None:
        raise RuntimeError("queue is full")


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
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )


def handoff(agent_id: int = 6984) -> Handoff:
    return Handoff(
        agent_id=agent_id,
        instruction=HANDOFF_INSTRUCTION,
        machine="macmini",
        workspace=Path("/agents/6984"),
        home=Path("/meiva-home"),
    )


async def test_a_first_turn_co_delivers_the_handoff_in_a_single_submission() -> None:
    session = FakeSession()
    doc = handoff()
    bridge = make_bridge(session, handoff=doc)
    turn = await run_turn(bridge, "hello", key="inbound-7")

    assert turn.text == "reply-t1"
    assert turn.usage.input_tokens == 10
    assert session.submitted == [(f"{doc.render().strip()}\n\nhello", "inbound-7", "t1")]
    assert bridge.model == "claude-sonnet-5-5"


async def test_a_turn_whose_text_already_starts_with_the_handoff_is_not_prefixed_twice() -> None:
    session = FakeSession()
    doc = handoff()
    bridge = make_bridge(session, handoff=doc)
    co_delivered = f"{doc.render().strip()}\n\nhello"

    await run_turn(bridge, co_delivered, key="inbound-1")
    await run_turn(bridge, "two", key="inbound-2")
    assert session.submitted == [
        (co_delivered, "inbound-1", "t1"),
        ("two", "inbound-2", "t2"),
    ]


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
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )

    with pytest.raises(HarnessTurnFaultedError, match="cli died"):
        await run_turn(bridge, "hello", key="inbound-1")

    assert faulted.closed, "a faulted session is dropped, not reused for the next inbound"
    turn = await run_turn(bridge, "again", key="inbound-2")

    assert turn.text == "reply-t1", "the rebuilt session runs its own first turn"
    assert fresh.started == 1
    assert fresh.submitted == [("again", "inbound-2", "t1")]


async def test_a_providers_own_terminal_message_does_not_truncate_the_turn() -> None:
    session = FakeSession(
        during=[UnsolicitedOutput(source="task-notification", text="its own answer")]
    )
    bridge = make_bridge(session)

    turn = await run_turn(bridge, "hello", key="inbound-1")

    assert turn.text == "reply-t1", "the provider's own reply is not this turn's text"
    assert not session.closed


async def test_a_session_failure_ends_the_wait_as_a_closed_session() -> None:
    session = FakeSession(during=[SessionFailed(error="the provider stream died")])
    bridge = make_bridge(session)

    with pytest.raises(HarnessClosedError, match="provider stream died"):
        await run_turn(bridge, "hello", key="inbound-1")

    assert session.closed, "a dead session is retired, never reused"


async def test_a_retired_fault_naming_no_turn_is_not_blamed_on_the_turn() -> None:
    # meiva:note
    # The retired shape an adapter that has not migrated still emits. With no turn
    # named it can only be the session's failure, so it must not settle as this
    # turn's own verdict (#5663).
    # meiva:link base/lm/harness/docs/harness.md
    session = FakeSession(during=[SessionFaulted(error="stream died")])
    bridge = make_bridge(session)

    with pytest.raises(HarnessClosedError, match="stream died"):
        await run_turn(bridge, "hello", key="inbound-1")

    assert session.closed


async def test_the_handoff_is_injected_once_and_the_session_started_once() -> None:
    session = FakeSession()
    doc = handoff()
    bridge = make_bridge(session, handoff=doc)

    await run_turn(bridge, "one", key="inbound-1")
    await run_turn(bridge, "two", key="inbound-2")

    assert session.started == 1
    assert session.submitted == [
        (f"{doc.render().strip()}\n\none", "inbound-1", "t1"),
        ("two", "inbound-2", "t2"),
    ]


async def test_calling_set_handoff_again_on_a_warm_session_does_not_reinject_the_handoff() -> None:
    session = FakeSession()
    doc = handoff()
    bridge = make_bridge(session, handoff=doc)

    await run_turn(bridge, "one", key="inbound-1")
    bridge.set_handoff(doc)
    await run_turn(bridge, "two", key="inbound-2")

    assert session.submitted == [
        (f"{doc.render().strip()}\n\none", "inbound-1", "t1"),
        ("two", "inbound-2", "t2"),
    ]


async def test_a_rebuilt_unresumed_session_after_fault_accepts_a_new_handoff_once() -> None:
    faulted, fresh = FakeSession(fault="cli died"), FakeSession()
    sessions = [faulted, fresh]
    doc = handoff()
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        6984,
        lambda _handle: sessions.pop(0),
        machine="macmini",
        handoff=doc,
        registration_ttl_seconds=3600.0,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )

    with pytest.raises(HarnessTurnFaultedError, match="cli died"):
        await run_turn(bridge, "hello", key="inbound-1")

    bridge.set_handoff(doc)
    await run_turn(bridge, "two", key="inbound-2")
    bridge.set_handoff(doc)
    await run_turn(bridge, "three", key="inbound-3")

    assert fresh.submitted == [
        (f"{doc.render().strip()}\n\ntwo", "inbound-2", "t1"),
        ("three", "inbound-3", "t2"),
    ]


async def test_a_faulted_session_fails_the_turn() -> None:
    bridge = make_bridge(FakeSession(fault="cli died"))

    with pytest.raises(HarnessTurnFaultedError, match="cli died"):
        await run_turn(bridge, "hello", key="inbound-1")


async def test_a_session_that_cannot_start_fails_the_turn_as_a_setup_error() -> None:
    def refuse(_handle: str) -> FakeSession:
        raise RuntimeError("session setup failed")

    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        6984,
        refuse,
        machine="macmini",
        registration_ttl_seconds=3600.0,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )

    with pytest.raises(HarnessSetupError, match="could not start"):
        await run_turn(bridge, "hello", key="inbound-1")


async def test_a_refused_submission_fails_the_turn_and_retires_the_session() -> None:
    session = RefusingSession()
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        6984,
        lambda _handle: session,
        machine="macmini",
        registration_ttl_seconds=3600.0,
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )

    with pytest.raises(HarnessSetupError, match="refused the input"):
        await run_turn(bridge, "hello", key="inbound-1")

    assert session.closed, "a session that refused a submission is not reused"


async def test_close_closes_the_session_and_refuses_later_turns() -> None:
    session = FakeSession()
    bridge = make_bridge(session)

    await run_turn(bridge, "hello", key="inbound-1")
    await bridge.close(reason="evicted")

    assert session.closed
    assert session.close_reason == "evicted"
    with pytest.raises(HarnessClosedError):
        await run_turn(bridge, "again", key="inbound-2")


@pytest.mark.parametrize(
    ("harness", "model", "effort"),
    [
        (Harness.CLAUDE_CODE, "claude-opus-5-5", "low"),
        (Harness.CODEX, "gpt-6.1-sol", "xhigh"),
        (Harness.ANTIGRAVITY, "gemini-4-argon-low", "low"),
    ],
)
def test_execution_selection_reaches_the_adapter(
    tmp_path: Path,
    harness: Harness,
    model: str,
    effort: str,
) -> None:
    binary_dir = tmp_path / "bin"
    binary_dir.mkdir()
    for name in ("claude", "codex"):
        binary = binary_dir / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
    received: list[UnsolicitedOutput] = []
    session = open_session(
        harness,
        agent_id=71,
        cwd=tmp_path,
        registry=FileSessionRegistry(tmp_path / "sessions"),
        environ={"HOME": str(tmp_path), "PATH": str(binary_dir)},
        clock=system_clock(),
        model=model,
        reasoning_effort=effort,
        unsolicited=received.append,
    )
    assert isinstance(session, (ClaudeSession, CodexSession, AgySession))
    session._unsolicited(UnsolicitedOutput(source="probe", text="hello"))
    assert received == [UnsolicitedOutput(source="probe", text="hello")]
    assert session.config.model == model
    if isinstance(session, AgySession):
        command = session.config.command(resume=None)
        assert command[command.index("--model") + 1] == model
    else:
        assert session.config.reasoning_effort == effort


def test_the_built_in_loop_has_no_bridge() -> None:
    assert (
        bridge_for(
            Harness.MEIVA,
            agent_id=1,
            machine="macmini",
            workspace=Path("/agents/6984"),
            home=Path("/meiva-home"),
            registry=FileSessionRegistry(Path("/registry")),
            registration_ttl_seconds=3600.0,
            environ={},
            root=bridge_root_values(),
        )
        is None
    )


def test_an_external_harness_builds_its_handoff() -> None:
    bridge = bridge_for(
        Harness.CLAUDE_CODE,
        agent_id=6984,
        machine="macmini",
        workspace=Path("/agents/6984"),
        home=Path("/meiva-home"),
        registry=FileSessionRegistry(Path("/registry")),
        registration_ttl_seconds=3600.0,
        environ={},
        root=bridge_root_values(),
    )
    assert bridge is not None
    assert bridge.harness is Harness.CLAUDE_CODE


# meiva:note
# Every harness the enum names now has an adapter, so the refusal in `open_session`
# guards the next value someone adds rather than a current one; the tests below pin
# what each adapter is built with instead. A new harness member with no branch fails
# there, loudly, before a session starts with another provider's flags.
# meiva:link base/lm/harness/docs/harness.md
def test_the_antigravity_wake_document_carries_its_shell_semantics() -> None:
    bridge = bridge_for(
        Harness.ANTIGRAVITY,
        agent_id=6984,
        machine="macmini",
        workspace=Path("/agents/6984"),
        home=Path("/meiva-home"),
        registry=FileSessionRegistry(Path("/registry")),
        registration_ttl_seconds=3600.0,
        environ={},
        root=bridge_root_values(),
    )
    assert bridge is not None
    assert bridge.handoff is not None
    rendered = bridge.handoff.render()
    assert "setsid" in rendered
    assert "fresh shell" in rendered
    assert "WaitMsBeforeAsync=10000" in rendered


def test_the_codex_wake_document_carries_its_unicode_input_workaround() -> None:
    bridge = bridge_for(
        Harness.CODEX,
        agent_id=6984,
        machine="macmini",
        workspace=Path("/agents/6984"),
        home=Path("/meiva-home"),
        registry=FileSessionRegistry(Path("/registry")),
        registration_ttl_seconds=3600.0,
        environ={},
        root=bridge_root_values(),
    )
    assert bridge is not None
    assert bridge.handoff is not None
    rendered = bridge.handoff.render()
    assert "paste(text, {format: 'text'})" in rendered
    assert "read back the field" in rendered


def test_an_antigravity_session_is_built_with_the_skip_flag_it_was_given() -> None:
    session = open_session(
        Harness.ANTIGRAVITY,
        agent_id=1,
        cwd=Path("/agents/6984"),
        registry=FileSessionRegistry(Path("/registry")),
        skip_permissions=True,
        environ={},
        clock=system_clock(),
    )
    assert isinstance(session, AgySession)
    assert session.config.skip_permissions is True

    plain = open_session(
        Harness.ANTIGRAVITY,
        agent_id=1,
        cwd=Path("/agents/6984"),
        registry=FileSessionRegistry(Path("/registry")),
        skip_permissions=False,
        environ={},
        clock=system_clock(),
    )
    assert isinstance(plain, AgySession)
    assert plain.config.skip_permissions is False


def test_each_adapter_names_the_harness_it_keeps_records_for() -> None:
    assert ClaudeSession.harness == Harness.CLAUDE_CODE.value
    assert CodexSession.harness == Harness.CODEX.value
    assert AgySession.harness == Harness.ANTIGRAVITY.value


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
        clock=system_clock(),
        handle_rng=SecureRandom(random.SystemRandom()),
        controller_pid=os.getpid(),
    )

    asyncio.run(run_turn(bridge, "hello", key="inbound-1"))

    registration = store.read(6984)
    assert registration is not None
    assert registration.expires_at is not None, "a row without an expiry outlives its host"
    assert registration.external_session_id == "session-1"
    assert registration.machine == "macmini"
    assert registration.handle_sha256 == handle_digest(handles[0]), "only the digest lands on disk"
    assert handles[0] not in registration.handle_sha256

    asyncio.run(bridge.close(reason="evicted"))
    assert store.read(6984) is None


# meiva:note
# The three values a bridge reads from outside are injected, and each lands in the row: the clock
# stamps `created_at` and `expires_at`, the controller pid is written as given, and the handle is
# drawn from the generator (32 bytes, 43 unpadded characters) whose digest alone is stored.
def test_the_registration_carries_the_injected_clock_pid_and_handle_generator(
    tmp_path: Path,
) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    handles: list[str] = []
    bridge = HarnessBridge(
        Harness.CLAUDE_CODE,
        6984,
        lambda handle: handles.append(handle) or FakeSession(),
        machine="macmini",
        registrations=store,
        registration_ttl_seconds=3600.0,
        clock=clock_at(datetime(2031, 5, 17, 9, 30, tzinfo=UTC)),
        handle_rng=SecureRandom(seeded_rng(7)),
        controller_pid=424242,
    )

    asyncio.run(run_turn(bridge, "hello", key="inbound-1"))

    registration = store.read(6984)
    assert registration is not None
    assert registration.controller_pid == 424242
    assert registration.created_at == "2031-05-17T09:30:00+00:00"
    assert registration.expires_at >= "2031-05-17T10:30:00+00:00", (
        "renewed for the session, never shorter"
    )
    drawn = base64.urlsafe_b64encode(seeded_rng(7).randbytes(32)).rstrip(b"=").decode()
    assert handles == [drawn]
    assert len(handles[0]) == 43
    assert registration.handle_sha256 == handle_digest(drawn)


def test_a_registration_expires_within_the_configured_ttl(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    bridge = make_bridge(FakeSession(), registrations=store)
    before = datetime.now(UTC)
    asyncio.run(run_turn(bridge, "hello", key="inbound-1"))
    after = datetime.now(UTC)

    written = store.read(6984)
    assert written is not None
    expires = datetime.fromisoformat(written.expires_at)
    ttl = timedelta(seconds=3600)
    assert before + ttl <= expires <= after + ttl


async def test_every_turn_renews_the_row_without_restamping_its_creation(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    bridge = make_bridge(FakeSession(), registrations=store)

    await run_turn(bridge, "one", key="inbound-1")
    first = store.read(6984)
    await run_turn(bridge, "two", key="inbound-2")
    second = store.read(6984)

    assert first is not None and second is not None
    assert second.created_at == first.created_at, "a renewal is not a new registration"
    assert second.expires_at >= first.expires_at, "each turn pushes the deadline forward"


def test_the_sweep_drops_a_registration_whose_controller_is_gone(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    store.write(registration(controller_pid=999_999_999))
    store.write(registration(agent_id=6985, controller_pid=os.getpid()))

    assert store.sweep(now=system_clock().now()) == [6984]
    assert store.read(6985) is not None


def test_the_sweep_drops_an_expired_registration(tmp_path: Path) -> None:
    store = RegistrationStore(tmp_path / "registrations")
    store.write(registration(expires_at="2000-01-01T00:00:00+00:00"))

    assert store.sweep(now=system_clock().now()) == [6984]


async def test_session_model_and_error_detail_surface_through_bridge() -> None:
    class ModelOnStartSession(FakeSession):
        model: str | None = "gpt-6.1-sol"

        async def start(self, *, resume: str | None = None) -> None:
            self.started += 1
            self.session_id = "session-1"

        def accept(self, text: str, *, key: str | None = None) -> None:
            self._n += 1
            turn_id = f"t{self._n}"
            if self._n == 2:
                self._queue.append(
                    replace(
                        completion(turn_id, ""),
                        subtype="failed",
                        is_error=True,
                        stop_reason="usage limit reached",
                    )
                )
            elif self._n == 3:
                self._queue.append(
                    replace(
                        completion(turn_id, ""),
                        is_error=True,
                        stop_reason="stop_sequence",
                        api_error_status=400,
                        errors=("API Error: 400 This API key is not scoped to a workspace",),
                    )
                )
            else:
                self._queue.append(completion(turn_id, "ok"))

    session = ModelOnStartSession()
    bridge = make_bridge(session)
    await run_turn(bridge, "one", key="k1")
    assert bridge.model == "gpt-6.1-sol"
    with pytest.raises(
        HarnessTurnFaultedError,
        match="failed \\(failed\\): harness reported a failure but gave no error detail"
        " \\(status/stop_reason: usage limit reached\\)",
    ):
        await run_turn(bridge, "two", key="k2")
    with pytest.raises(HarnessTurnFaultedError, match="failed \\(API 400\\): API Error: 400"):
        await run_turn(bridge, "three", key="k3")

    assert session.started == 1
    assert not session.closed


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
