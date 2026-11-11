from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from base.lm.harness.delivery import HarnessInput, Reconciliation
from base.lm.harness.events import BridgeEvent, TurnCompleted, Usage


# meiva:note
# What one harness turn produced, in the terms the graph already accounts for: the
# reply text, the provider usage the turn reported, and the turn boundary that
# closed it. It is built from the turn's `TurnCompleted` and the text streamed during it.
# meiva:link base/lm/harness/docs/harness.md
@dataclass(frozen=True, slots=True)
class HarnessTurn:
    turn_id: str
    text: str
    usage: Usage
    cost_usd: float | None
    subtype: str
    is_error: bool
    interrupted: bool
    stop_reason: str | None = None
    num_turns: int = 1
    steps: tuple[Usage, ...] = ()
    # meiva:note
    # What the provider said went wrong, when it said more than a subtype. `api_error_status` is the
    # HTTP status of a failed API call the result reports as `is_error` with `subtype="success"`
    # (the SDK documents that shape), and `errors` is what the CLI declares about the failure: its
    # own list, or the provider's sentence a failed API call carries in `result`. Both are read only
    # to build a fault message, and `errors` is that message's only source of detail; a turn that
    # succeeded carries neither.
    api_error_status: int | None = None
    errors: tuple[str, ...] = ()


def completed_turn(event: TurnCompleted, streamed: str) -> HarnessTurn:
    return HarnessTurn(
        turn_id=event.turn_id,
        text=event.text or streamed,
        usage=event.usage,
        cost_usd=event.cost_usd,
        subtype=event.subtype,
        is_error=event.is_error,
        interrupted=event.interrupted,
        stop_reason=event.stop_reason,
        num_turns=max(event.num_turns, 1),
        steps=event.steps,
        api_error_status=event.api_error_status,
        errors=event.errors,
    )


# meiva:note
# The adapter contract the bridge consumes, stated structurally: the three
# adapters share these member shapes without sharing a base class, and a test fake
# needs only to look like one. `deliver` hands inputs to the foreground and returns when they
# are handed over; everything that happens to them comes back on `events()`.
# meiva:link base/lm/harness/docs/harness.md
class HarnessSession(Protocol):
    def start(self, *, resume: str | None = None) -> Awaitable[None]: ...
    def deliver(self, inputs: Sequence[HarnessInput]) -> Awaitable[None]: ...
    def reconcile(self, keys: Sequence[str]) -> Awaitable[Mapping[str, Reconciliation]]: ...
    def adopt(self, keys: Sequence[str]) -> None: ...
    def events(self) -> AsyncIterator[BridgeEvent]: ...
    def interrupt(self, *, turn_id: str | None = None) -> Awaitable[object]: ...
    def close(self, *, reason: str = "closed") -> Awaitable[None]: ...

    @property
    def session_id(self) -> str | None: ...

    @property
    def generation(self) -> int: ...

    # meiva:note
    # The three states are the whole contract: None while the CLI child is still
    # running or was never observable, 0 when it exited normally, non-zero when it
    # exited abnormally. A session that has no child of its own (none today, but the
    # built-in loop is not a session) must answer None rather than invent a 0 — a
    # faked 0 is indistinguishable from a clean exit forever after.
    @property
    def exit_code(self) -> int | None: ...


# meiva:note
# What the graph reads of a subscription: the next event, or None once the stream has ended, and a way
# to stop watching. `pump.Subscription` is the implementation.
# meiva:link base/lm/harness/docs/delivery.md
class EventSubscription(Protocol):
    @property
    def unread(self) -> bool: ...

    def get(self) -> Awaitable[BridgeEvent | None]: ...
    def close(self) -> None: ...


# meiva:note
# What a graph run needs from a live external session: watch its events, hand it inputs, interrupt the
# foreground, retire or close the session. The graph never reaches into an adapter, and it never asks
# "did that input arrive": every outcome of an input comes back as an event on a subscription, so a
# caller subscribes first and delivers second. `notify_inbound` / `wait_inbound` carry the host's
# "new inbound for this agent" wake to a graph invocation that is already running.
# meiva:link base/lm/harness/docs/delivery.md
class TurnEngine(Protocol):
    @property
    def model(self) -> str | None: ...

    @property
    def generation(self) -> int: ...

    @property
    def has_unpresented_turn(self) -> bool: ...

    def subscribe(self) -> Awaitable[EventSubscription]: ...
    def deliver(self, inputs: Sequence[HarnessInput]) -> Awaitable[None]: ...
    def reconcile(self, keys: Sequence[str]) -> Awaitable[Mapping[str, Reconciliation]]: ...
    def adopt(self, keys: Sequence[str]) -> None: ...
    def interrupt(self) -> Awaitable[object]: ...
    def retire(self, reason: str) -> Awaitable[None]: ...
    def close(self, *, reason: str = "closed") -> Awaitable[None]: ...
    def notify_inbound(self) -> None: ...
    def wait_inbound(self, timeout: float) -> Awaitable[None]: ...

    # meiva:note
    # The same three states as the adapter's `exit_code`, asked of the engine the
    # graph holds: it is what a retiring host records on the agent row. Read it
    # AFTER `close()` — before that the child may still be running, and the answer
    # would be a stale None rather than the truth.
    @property
    def exit_code(self) -> int | None: ...
