from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from base.lm.harness.events import BridgeEvent, Usage
from base.lm.harness.handoff import Handoff


# meiva:note
# What one harness turn produced, in the terms the graph already accounts for: the
# reply text, the provider usage the turn reported, and the turn boundary that
# closed it. `prelude` holds the turns that completed earlier in the same wait: a
# session started with a handoff runs that document as its first turn, so the waiter
# sees two completions. Each one keeps its own text and usage, which is what lets the
# graph commit the handoff turn as its own message instead of folding it into the
# reply — normally the prelude is empty.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
@dataclass(frozen=True, slots=True)
class HarnessTurn:
    turn_id: str
    text: str
    usage: Usage
    cost_usd: float | None
    subtype: str
    is_error: bool
    interrupted: bool
    num_turns: int = 1
    steps: tuple[Usage, ...] = ()
    prelude: tuple[HarnessTurn, ...] = ()
    replayed: bool = False


# meiva:note
# The adapter contract the bridge consumes, stated structurally: the three
# adapters share these member shapes without sharing a base class, and a test fake
# needs only to look like one. `submit` returns an adapter-owned ticket because
# tickets carry their turn id and an outcome for completed-key replay. A fresh
# session exposes its handoff ticket so the bridge can await it before user work.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
class HarnessSession(Protocol):
    async def start(self, *, resume: str | None = None, handoff: Handoff | None = None) -> None: ...
    def submit(self, text: str, *, key: str | None = None) -> Any: ...
    def events(self) -> AsyncIterator[BridgeEvent]: ...
    async def interrupt(self) -> Any: ...
    async def close(self, *, reason: str = "closed") -> None: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def handoff_ticket(self) -> Any | None: ...

    # meiva:note
    # The three states are the whole contract: None while the CLI child is still
    # running or was never observable, 0 when it exited normally, non-zero when it
    # exited abnormally. A session that has no child of its own (none today, but the
    # built-in loop is not a session) must answer None rather than invent a 0 — a
    # faked 0 is indistinguishable from a clean exit forever after.
    @property
    def exit_code(self) -> int | None: ...


# meiva:note
# What a graph run needs from a live external session: run one turn, interrupt the
# running one, close the session. The graph never reaches into an adapter; a
# context that carries no engine runs the built-in loop.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
class TurnEngine(Protocol):
    @property
    def model(self) -> str | None: ...

    async def run_turn(
        self,
        text: str,
        *,
        key: str,
        on_event: Callable[[BridgeEvent], None] | None = None,
    ) -> HarnessTurn: ...
    async def interrupt(self) -> Any: ...
    async def close(self, *, reason: str = "closed") -> None: ...

    # meiva:note
    # The same three states as the adapter's `exit_code`, asked of the engine the
    # graph holds: it is what a retiring host records on the agent row. Read it
    # AFTER `close()` — before that the child may still be running, and the answer
    # would be a stale None rather than the truth.
    @property
    def exit_code(self) -> int | None: ...
