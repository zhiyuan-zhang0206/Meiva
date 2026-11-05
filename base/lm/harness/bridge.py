from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import timedelta

from base.api_contracts.agent_contract import Harness
from base.lm.harness.base_session import SessionClock
from base.lm.harness.engine import (
    CompletedTurnEvent,
    ForegroundSteering,
    HarnessSession,
    HarnessTurn,
    SubmittedTurn,
    TurnEventSink,
)
from base.lm.harness.events import (
    AssistantText,
    BridgeEvent,
    ForegroundAccepted,
    ReasoningDelta,
    ReasoningText,
    SessionClosed,
    SessionFailed,
    SessionFaulted,
    SessionStarted,
    TextDelta,
    ToolResult,
    ToolUse,
    TurnCompleted,
    TurnFaulted,
    UnsolicitedOutput,
)
from base.lm.harness.faults import fault_message
from base.lm.harness.handoff import Handoff
from base.lm.harness.registration import (
    ATTACHED,
    HarnessRegistration,
    RegistrationStore,
    SecureRandom,
    handle_digest,
)
from base.lm.harness.registry import SessionRecord, SessionRegistry
from base.log import logger


class HarnessTurnFaultedError(RuntimeError):
    def __init__(self, message: str, *, outcome: HarnessTurn | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome


class HarnessClosedError(RuntimeError):
    pass


# meiva:note
# A turn that never got a session, or that the session refused to accept. It is a
# different failure from a turn that died mid-flight: nothing was sent, so there is no
# half-written transcript to reconcile — but from the graph's side both mean the same
# thing, and both settle the turn as a reported abort rather than a host crash.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
class HarnessSetupError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScopedSessionRegistry:
    # meiva:note
    # The registry as one harness sees it: every load is that harness's, every save names
    # it. An adapter takes a registry and would otherwise have to remember which harness it
    # is; the scope makes the answer structural, and a record written by another harness is
    # refused at load instead of resuming a transcript the provider cannot read.
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
    inner: SessionRegistry
    harness: Harness

    def load(self, agent_id: int, *, harness: str | None = None) -> SessionRecord | None:
        if harness is not None and harness != self.harness.value:
            raise ValueError(
                f"this registry is scoped to harness {self.harness.value!r}, not {harness!r}"
            )
        return self.inner.load(agent_id, harness=self.harness.value)

    def save(self, record: SessionRecord) -> None:
        self.inner.save(replace(record, harness=self.harness.value))

    def delete(self, agent_id: int) -> bool:
        return self.inner.delete(agent_id)


class HarnessBridge:
    # meiva:note
    # The turn engine the graph hands a message to. It owns one session, started on the
    # first turn and resumed by the adapter from the registry, and it is the only
    # consumer of that session's event stream: the graph runs one turn at a time per
    # agent, so a second reader would split the stream rather than add one.
    # On a fresh session, the handoff document is co-delivered in the first turn's
    # submission rather than run as a separate prelude turn.
    # meiva:link agent/graph/docs/harness-turn.meiva.okf.md
    def __init__(
        self,
        harness: Harness,
        agent_id: int,
        factory: Callable[[str], HarnessSession],
        *,
        machine: str = "",
        handoff: Handoff | None = None,
        registrations: RegistrationStore | None = None,
        registration_ttl_seconds: float,
        clock: SessionClock,
        handle_rng: SecureRandom,
        controller_pid: int,
    ) -> None:
        self.harness = harness
        self._agent_id = agent_id
        self._factory = factory
        self._machine = machine
        self._handoff = handoff
        self._registrations = registrations
        self._clock = clock
        self._handle_rng = handle_rng
        self._controller_pid = controller_pid
        if registration_ttl_seconds <= 0:
            raise ValueError(f"a harness registration must expire, got {registration_ttl_seconds}s")
        self._registration_ttl = timedelta(seconds=registration_ttl_seconds)
        self._created_at: str | None = None
        self._session: HarnessSession | None = None
        self._foreground: asyncio.Future[tuple[HarnessSession, str] | None] | None = None
        self._handoff_delivered = False
        self._model: str | None = None
        self._digest: str | None = None
        self._closed = False
        self._close_error: BaseException | None = None
        # meiva:note
        # The exit status of the session this bridge retired last (three states: None
        # while a session runs or none was ever started, 0 normal, non-zero abnormal).
        # Recorded on the agent row by the host at retirement (task #5616).
        self._exit_code: int | None = None

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def handoff(self) -> Handoff | None:
        return self._handoff

    def set_handoff(self, handoff: Handoff) -> None:
        self._handoff = handoff

    @property
    def session_id(self) -> str | None:
        return None if self._session is None else self._session.session_id

    async def ensure(self) -> HarnessSession:
        if self._close_error is not None:
            raise HarnessClosedError(
                "the previous harness session could not close"
            ) from self._close_error
        if self._session is not None:
            return self._session
        if self._closed:
            raise HarnessClosedError("this harness bridge is closed")
        handle = base64.urlsafe_b64encode(self._handle_rng.randbytes(32)).rstrip(b"=").decode()
        self._digest = handle_digest(handle)
        self._register("")
        session = self._factory(handle)
        try:
            await session.start()
        except BaseException:
            await self._retire(session, "session start failed")
            raise
        self._exit_code = None
        self._session = session
        session_model = getattr(session, "model", None)
        if isinstance(session_model, str) and session_model:
            self._model = session_model
        self._handoff_delivered = bool(getattr(session, "resumed", False))
        return session

    @property
    def steering_available(self) -> bool:
        session = self._session
        return isinstance(session, ForegroundSteering) and session.steering_available

    async def steer(self, text: str, *, key: str) -> bool:
        session = self._session
        if not isinstance(session, ForegroundSteering):
            return False
        return await session.steer(text, key=key)

    async def run_turn(
        self,
        text: str,
        *,
        key: str,
        on_event: TurnEventSink | None = None,
    ) -> HarnessTurn:
        if self._foreground is not None:
            raise HarnessSetupError("a harness turn is already running")
        foreground: asyncio.Future[tuple[HarnessSession, str] | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._foreground = foreground
        try:
            return await self._run_turn(text, key, on_event, foreground)
        finally:
            if not foreground.done():
                foreground.set_result(None)
            self._foreground = None

    async def _run_turn(
        self,
        text: str,
        key: str,
        on_event: TurnEventSink | None,
        foreground: asyncio.Future[tuple[HarnessSession, str] | None],
    ) -> HarnessTurn:
        try:
            session = await self.ensure()
        except HarnessClosedError:
            raise
        except Exception as exc:
            raise HarnessSetupError(f"{self.harness.value} session could not start: {exc}") from exc
        self._register(session.session_id or "")
        handoff, self._handoff = self._handoff, None
        if handoff is not None and not self._handoff_delivered:
            rendered = handoff.render().strip()
            if not text.startswith(rendered):
                text = f"{rendered}\n\n{text}" if text else rendered
        self._handoff_delivered = True
        try:
            try:
                ticket: SubmittedTurn = session.submit(text, key=key)
            except Exception as exc:
                raise HarnessSetupError(f"{self.harness.value} refused the turn: {exc}") from exc
            if ticket.deduplicated and ticket.settled:
                return replace(_completed(await ticket.outcome(), ""), replayed=True)
            turn_id = str(ticket.turn_id)
            foreground.set_result((session, turn_id))
            return await self._wait_with_registration(session, turn_id, on_event)
        except HarnessTurnFaultedError as exc:
            if exc.outcome is None:
                await self._retire(session, reason="unsettled turn fault")
            raise
        except (HarnessClosedError, HarnessSetupError):
            await self._retire(session, reason="session unavailable")
            raise

    # meiva:note
    # The renewal has the turn's lifetime. Preserve the original turn exception so
    # callers can distinguish cancellation, an adapter fault, and a session closure.
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
    async def _wait_with_registration(
        self, session: HarnessSession, turn_id: str, on_event: TurnEventSink | None
    ) -> HarnessTurn:
        if self._registrations is None:
            return await self._await_turn(session, turn_id, on_event=on_event)

        outcome: HarnessTurn | BaseException
        async with asyncio.TaskGroup() as tasks:
            renewal = tasks.create_task(
                self._renew_registration(session), name=f"harness-registration-{self._agent_id}"
            )
            try:
                outcome = await self._await_turn(session, turn_id, on_event=on_event)
            except BaseException as error:
                outcome = error
            finally:
                _ = renewal.cancel()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def interrupt(self) -> object:
        foreground = self._foreground
        if foreground is None:
            return None
        target = await asyncio.shield(foreground)
        if target is None or self._foreground is not foreground:
            return None
        session, turn_id = target
        return await session.interrupt(turn_id=turn_id)

    async def close(self, *, reason: str = "closed") -> None:
        if self._close_error is not None:
            raise HarnessClosedError(
                "the previous harness session could not close"
            ) from self._close_error
        if self._closed:
            return
        self._closed = True
        if self._session is not None:
            await self._retire(self._session, reason)
        else:
            self._retire_registration()

    # meiva:note
    # A session that faults or closes mid-turn is dropped rather than reused, and the next
    # inbound rebuilds one from the registry — the provider still holds the transcript, so a
    # crashed CLI costs one turn instead of every later one. Retirement is not `close`: the
    # bridge stays open for that next turn, which is why the two are separate calls.
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
    async def _retire(self, session: HarnessSession, reason: str) -> None:
        if self._session is session:
            self._session = None
            self._handoff_delivered = False
            self._created_at = None
        self._retire_registration()
        try:
            await session.close(reason=reason)
        except BaseException as error:
            self._close_error = error
            raise
        # meiva:note
        # Only a session that closed reports an exit status; a failed close leaves
        # the field at None rather than inventing one.
        self._exit_code = session.exit_code

    # meiva:note
    # Every row expires so a reused controller pid cannot inherit a crashed host's
    # bearer capability. A running turn renews its registration independently of provider
    # output. The identity check prevents an interrupt or close from resurrecting a row.
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
    async def _renew_registration(self, session: HarnessSession) -> None:
        interval = self._registration_ttl.total_seconds() / 3
        while self._session is session:
            await self._clock.asleep(interval)
            if self._session is not session:
                return
            try:
                self._register(session.session_id or "")
            except OSError as exc:
                logger.warning(
                    "could not renew harness registration for agent {agent_id}: {error}",
                    event="harness_registration_renewal_failed",
                    agent_id=self._agent_id,
                    error=exc,
                )

    # meiva:note
    # A renewal preserves the handle, controller and creation time for this session.
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
    def _register(self, external_session_id: str) -> None:
        if self._registrations is None or self._digest is None:
            return
        now = self._clock.now()
        self._created_at = self._created_at or now.isoformat()
        self._registrations.write(
            HarnessRegistration(
                agent_id=self._agent_id,
                harness=self.harness.value,
                external_session_id=external_session_id,
                machine=self._machine,
                controller_pid=self._controller_pid,
                handle_sha256=self._digest,
                created_at=self._created_at,
                expires_at=(now + self._registration_ttl).isoformat(),
                status=ATTACHED,
            )
        )

    def _retire_registration(self) -> None:
        if self._registrations is not None:
            self._registrations.delete(self._agent_id)

    async def _await_turn(
        self,
        session: HarnessSession,
        turn_id: str,
        *,
        on_event: TurnEventSink | None = None,
    ) -> HarnessTurn:
        streamed: dict[str, list[str]] = {}
        async for event in session.events():
            done = await self._consume_event(event, turn_id, streamed, on_event=on_event)
            if done is not None:
                if done.is_error and not done.interrupted:
                    raise HarnessTurnFaultedError(
                        fault_message(self.harness.value, done), outcome=done
                    )
                return done
        raise HarnessClosedError(f"harness event stream ended before turn {turn_id} completed")

    async def _consume_event(
        self,
        event: BridgeEvent,
        turn_id: str,
        streamed: dict[str, list[str]],
        *,
        on_event: TurnEventSink | None = None,
    ) -> HarnessTurn | None:
        if isinstance(event, SessionStarted):
            if event.model:
                self._model = event.model
            self._register(event.session_id)
            return None
        if isinstance(event, TurnCompleted):
            if event.turn_id == turn_id:
                return _completed(event, "".join(streamed.get(event.turn_id, [])))
            return None
        if isinstance(event, AssistantText):
            streamed.setdefault(event.turn_id, []).append(event.text)
        if await self._forward(event, turn_id, on_event):
            return None
        _raise_if_faulted(event, turn_id)
        return None

    async def _forward(
        self,
        event: BridgeEvent,
        turn_id: str,
        on_event: TurnEventSink | None,
    ) -> bool:
        # meiva:note
        # Streaming payload reaches the caller as it arrives, but only when the caller
        # asked to watch and the payload names the turn being run. Anything else is
        # another turn's speech: this turn neither delivers it nor is blamed for it
        # (#5663).
        # meiva:link base/lm/harness/docs/attribution.meiva.okf.md
        if on_event is None:
            return False
        if not isinstance(
            event,
            (
                ForegroundAccepted,
                AssistantText,
                ReasoningDelta,
                ReasoningText,
                TextDelta,
                ToolUse,
                ToolResult,
            ),
        ):
            return False
        if event.turn_id != turn_id:
            return False
        if (pending := on_event(event)) is not None:
            await pending
        return True


# meiva:note
# Only a failure that names this turn can end it. A session-level failure ends
# the turn too, but as what it is — the session is gone — and never as this
# turn's own verdict; a failure or a report that names another turn is not
# ours to act on, and neither is output the provider produced for a turn we
# never ran (#5663).
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def _raise_if_faulted(event: BridgeEvent, turn_id: str) -> None:
    if isinstance(event, TurnFaulted):
        if event.turn_id == turn_id:
            raise HarnessTurnFaultedError(event.error)
        return
    if isinstance(event, SessionFailed):
        raise HarnessClosedError(f"harness session failed: {event.error}")
    if isinstance(event, UnsolicitedOutput):
        return
    if isinstance(event, SessionFaulted):
        # meiva:note
        # The retired shape, emitted by an adapter that has not migrated yet. It
        # splits the same way: naming a turn is turn-scoped, naming none is the
        # session's own failure. It is checked last because the two events above
        # are also instances of it.
        # meiva:link base/lm/harness/docs/harness.meiva.okf.md
        if event.turn_id is None:
            raise HarnessClosedError(f"harness session failed: {event.error}")
        if event.turn_id == turn_id:
            raise HarnessTurnFaultedError(event.error)
        return
    if isinstance(event, SessionClosed):
        raise HarnessClosedError(
            f"harness session closed before turn {turn_id} completed: {event.reason}"
        )


def _completed(event: CompletedTurnEvent, streamed: str) -> HarnessTurn:
    return HarnessTurn(
        turn_id=event.turn_id,
        text=event.text or streamed,
        usage=event.usage,
        cost_usd=event.cost_usd,
        subtype=event.subtype,
        is_error=event.is_error,
        interrupted=event.interrupted,
        stop_reason=getattr(event, "stop_reason", None),
        num_turns=max(int(getattr(event, "num_turns", 1) or 1), 1),
        steps=tuple(getattr(event, "steps", ()) or ()),
        api_error_status=getattr(event, "api_error_status", None),
        errors=tuple(getattr(event, "errors", ()) or ()),
    )
