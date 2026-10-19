from __future__ import annotations

import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from base.api_contracts.agent_contract import Harness
from base.lm.harness.engine import HarnessSession, HarnessTurn
from base.lm.harness.events import (
    AssistantText,
    SessionClosed,
    SessionFaulted,
    SessionStarted,
    TurnCompleted,
    Usage,
)
from base.lm.harness.handoff import Handoff
from base.lm.harness.registration import (
    HarnessRegistration,
    RegistrationStore,
    handle_digest,
)
from base.lm.harness.registry import SessionRecord, SessionRegistry
from base.log import logger


class HarnessTurnFaultedError(RuntimeError):
    pass


class HarnessClosedError(RuntimeError):
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
    # `run_turn` waits for the turn its submission created and ignores the others; a
    # session that opened with a handoff reports that turn's completion first, and its
    # usage is counted with the turn being waited for rather than dropped.
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
    ) -> None:
        self.harness = harness
        self._agent_id = agent_id
        self._factory = factory
        self._machine = machine
        self._handoff = handoff
        self._registrations = registrations
        self._session: HarnessSession | None = None
        self._model: str | None = None
        self._digest: str | None = None
        self._closed = False

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def session_id(self) -> str | None:
        return None if self._session is None else self._session.session_id

    async def ensure(self) -> HarnessSession:
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
            await _close_quietly(session, "session start failed")
            self._retire_registration()
            raise
        self._handoff = None
        self._session = session
        return session

    async def run_turn(self, text: str, *, key: str) -> HarnessTurn:
        session = await self.ensure()
        ticket: Any = session.submit(text, key=key)
        return await self._await_turn(session, str(ticket.turn_id))

    async def interrupt(self) -> Any:
        if self._session is None:
            raise HarnessClosedError("no harness session is running")
        return await self._session.interrupt()

    async def close(self, *, reason: str = "closed") -> None:
        if self._closed:
            return
        self._closed = True
        session, self._session = self._session, None
        self._retire_registration()
        if session is not None:
            await session.close(reason=reason)

    def _register(self, external_session_id: str) -> None:
        if self._registrations is None or self._digest is None:
            return
        self._registrations.write(
            HarnessRegistration(
                agent_id=self._agent_id,
                harness=self.harness.value,
                external_session_id=external_session_id,
                machine=self._machine,
                controller_pid=os.getpid(),
                handle_sha256=self._digest,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                status="active",
            )
        )

    def _retire_registration(self) -> None:
        if self._registrations is not None:
            self._registrations.delete(self._agent_id)

    async def _await_turn(self, session: HarnessSession, turn_id: str) -> HarnessTurn:
        parts: list[str] = []
        usage = Usage()
        cost: float | None = None
        async for event in session.events():
            if isinstance(event, SessionStarted):
                self._model = event.model
                self._register(event.session_id)
            elif isinstance(event, TurnCompleted):
                usage = _add_usage(usage, event.usage)
                cost = _add_cost(cost, event.cost_usd)
                if event.turn_id == turn_id:
                    return HarnessTurn(
                        turn_id=turn_id,
                        text=event.text or "".join(parts),
                        usage=usage,
                        cost_usd=cost,
                        subtype=event.subtype,
                        is_error=event.is_error,
                        interrupted=event.interrupted,
                    )
            elif isinstance(event, AssistantText):
                if event.turn_id == turn_id:
                    parts.append(event.text)
            elif isinstance(event, SessionFaulted):
                if event.turn_id is None or event.turn_id == turn_id:
                    raise HarnessTurnFaultedError(event.error)
            elif isinstance(event, SessionClosed):
                raise HarnessClosedError(
                    f"harness session closed before turn {turn_id} completed: {event.reason}"
                )
        raise HarnessClosedError(f"harness event stream ended before turn {turn_id} completed")


def _add_usage(total: Usage, turn: Usage) -> Usage:
    return Usage(
        input_tokens=total.input_tokens + turn.input_tokens,
        output_tokens=total.output_tokens + turn.output_tokens,
        cache_creation_input_tokens=(
            total.cache_creation_input_tokens + turn.cache_creation_input_tokens
        ),
        cache_read_input_tokens=total.cache_read_input_tokens + turn.cache_read_input_tokens,
    )


def _add_cost(total: float | None, turn: float | None) -> float | None:
    if turn is None:
        return total
    return turn if total is None else total + turn


async def _close_quietly(session: HarnessSession, reason: str) -> None:
    try:
        await session.close(reason=reason)
    except Exception:
        logger.warning(
            "[{label}] {body}",
            label="harness",
            event="harness_close_failed",
            body=f"closing a session that failed to start raised ({reason})",
        )
