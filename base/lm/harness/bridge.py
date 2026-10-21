from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from base.api_contracts.agent_contract import Harness
from base.lm.harness.engine import HarnessSession, HarnessTurn
from base.lm.harness.events import (
    AssistantText,
    SessionClosed,
    SessionFaulted,
    SessionStarted,
    TurnCompleted,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.registration import (
    ATTACHED,
    HarnessRegistration,
    RegistrationStore,
    handle_digest,
)
from base.lm.harness.registry import SessionRecord, SessionRegistry


class HarnessTurnFaultedError(RuntimeError):
    pass


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
    # A fresh session finishes its handoff before user work is submitted, so cancelling
    # the handoff cannot promote an already queued user task. Its completion stays a
    # separate prelude with its own usage.
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
    ) -> None:
        self.harness = harness
        self._agent_id = agent_id
        self._factory = factory
        self._machine = machine
        self._handoff = handoff
        self._registrations = registrations
        if registration_ttl_seconds <= 0:
            raise ValueError(f"a harness registration must expire, got {registration_ttl_seconds}s")
        self._registration_ttl = timedelta(seconds=registration_ttl_seconds)
        self._created_at: str | None = None
        self._session: HarnessSession | None = None
        self._handoff_turn_id: str | None = None
        self._model: str | None = None
        self._digest: str | None = None
        self._closed = False
        self._close_error: BaseException | None = None

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def handoff(self) -> Handoff | None:
        return self._handoff

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
        handle = secrets.token_urlsafe(32)
        self._digest = handle_digest(handle)
        self._register("")
        session = self._factory(handle)
        try:
            await session.start(handoff=self._handoff)
        except BaseException:
            await self._retire(session, "session start failed")
            raise
        self._handoff = None
        self._session = session
        opening = session.handoff_ticket
        self._handoff_turn_id = None if opening is None else str(opening.turn_id)
        return session

    async def run_turn(self, text: str, *, key: str) -> HarnessTurn:
        try:
            session = await self.ensure()
        except HarnessClosedError:
            raise
        except Exception as exc:
            raise HarnessSetupError(f"{self.harness.value} session could not start: {exc}") from exc
        self._register(session.session_id or "")
        try:
            prelude = await self._await_handoff(session)
            try:
                ticket: Any = session.submit(text, key=key)
            except Exception as exc:
                raise HarnessSetupError(f"{self.harness.value} refused the turn: {exc}") from exc
            if ticket.deduplicated and ticket.settled:
                turn = replace(_completed(await ticket.outcome(), ""), replayed=True)
            else:
                turn = await self._await_turn(session, str(ticket.turn_id))
            return replace(turn, prelude=prelude)
        except (HarnessTurnFaultedError, HarnessClosedError, HarnessSetupError):
            await self._retire(session, reason="turn faulted")
            raise

    async def _await_handoff(self, session: HarnessSession) -> tuple[HarnessTurn, ...]:
        if self._handoff_turn_id is None:
            return ()
        opening = await self._await_turn(session, self._handoff_turn_id)
        self._handoff_turn_id = None
        if opening.is_error or opening.interrupted:
            raise HarnessTurnFaultedError(f"{self.harness.value} handoff did not complete")
        return (opening,)

    async def interrupt(self) -> Any:
        session = self._session
        if session is None:
            raise HarnessClosedError("no harness session is running")
        try:
            return await session.interrupt()
        finally:
            await self._retire(session, reason="interrupted")

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
            self._handoff_turn_id = None
            self._created_at = None
        self._retire_registration()
        try:
            await session.close(reason=reason)
        except BaseException as error:
            self._close_error = error
            raise

    # meiva:note
    # Every row carries an expiry, and the bridge rewrites it at the start of each turn:
    # `descends_from(controller_pid)` is only as good as the kernel's pid allocation, so
    # a row that never expired would be a bearer capability that a reused pid could
    # eventually inherit. Renewal is what keeps a live agent's row valid — the TTL is
    # sized above the adapter's own turn timeout — and it is also what makes a crashed
    # host's row lapse without anyone sweeping it.
    # meiva:link base/lm/harness/docs/harness.meiva.okf.md
    def _register(self, external_session_id: str) -> None:
        if self._registrations is None or self._digest is None:
            return
        now = datetime.now(UTC)
        self._created_at = self._created_at or now.isoformat(timespec="seconds")
        self._registrations.write(
            HarnessRegistration(
                agent_id=self._agent_id,
                harness=self.harness.value,
                external_session_id=external_session_id,
                machine=self._machine,
                controller_pid=os.getpid(),
                handle_sha256=self._digest,
                created_at=self._created_at,
                expires_at=(now + self._registration_ttl).isoformat(timespec="seconds"),
                status=ATTACHED,
            )
        )

    def _retire_registration(self) -> None:
        if self._registrations is not None:
            self._registrations.delete(self._agent_id)

    async def _await_turn(self, session: HarnessSession, turn_id: str) -> HarnessTurn:
        streamed: dict[str, list[str]] = {}
        async for event in session.events():
            if isinstance(event, SessionStarted):
                self._model = event.model
                self._register(event.session_id)
            elif isinstance(event, TurnCompleted):
                if event.turn_id == turn_id:
                    return _completed(event, "".join(streamed.get(event.turn_id, [])))
            elif isinstance(event, AssistantText):
                streamed.setdefault(event.turn_id, []).append(event.text)
            elif isinstance(event, SessionFaulted):
                if event.turn_id is None or event.turn_id == turn_id:
                    raise HarnessTurnFaultedError(event.error)
            elif isinstance(event, SessionClosed):
                raise HarnessClosedError(
                    f"harness session closed before turn {turn_id} completed: {event.reason}"
                )
        raise HarnessClosedError(f"harness event stream ended before turn {turn_id} completed")


def _completed(event: Any, streamed: str) -> HarnessTurn:
    return HarnessTurn(
        turn_id=event.turn_id,
        text=event.text or streamed,
        usage=event.usage,
        cost_usd=event.cost_usd,
        subtype=event.subtype,
        is_error=event.is_error,
        interrupted=event.interrupted,
    )
