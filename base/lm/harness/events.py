from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class SessionStarted:
    session_id: str
    model: str | None
    cwd: str | None
    api_key_source: str | None = None
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionBound:
    agent_id: int
    session_id: str
    resumed: bool
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class HandoffInjected:
    agent_id: int
    turn_id: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    turn_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ReasoningText:
    turn_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TextDelta:
    turn_id: str
    text: str


@dataclass(frozen=True, slots=True)
class AssistantText:
    turn_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolUse:
    turn_id: str
    tool_use_id: str
    name: str
    tool_input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    turn_id: str
    tool_use_id: str
    is_error: bool
    content: str


@dataclass(frozen=True, slots=True)
class RateLimitObserved:
    status: str
    utilization: float | None
    resets_at: int | None
    rate_limit_type: str | None
    turn_id: str | None = None


# meiva:note
# `input_tokens` is the whole input a turn was billed for, cache reads and cache
# writes included; the two cache fields are the subsets that priced differently.
# That is the shape `base/lm/pricing.quote` requires, and it is the one LangChain
# hands the built-in runtime. Providers disagree on the raw field — Anthropic
# reports the uncached remainder, OpenAI already totals — so each adapter folds
# its provider's numbers into this shape at its mapping boundary, and every
# consumer downstream can add them up without asking which harness produced them.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    turn_id: str
    session_id: str | None
    text: str
    subtype: str
    is_error: bool
    interrupted: bool
    stop_reason: str | None
    num_turns: int
    duration_ms: int
    usage: Usage
    cost_usd: float | None
    cumulative_cost_usd: float | None
    permission_denials: tuple[str, ...]
    steps: tuple[Usage, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionClosed:
    reason: str
    turn_id: str | None = None


# meiva:note
# A failure says what it knows about ownership, and nothing more. One
# `SessionFaulted(error, turn_id=None)` used to say both "this turn failed" and
# "the session failed", and its `turn_id=None` meant "whichever turn is running" —
# which is how an idle session's failure was read as the next turn's own outcome
# (#5663). The two events below cannot express that: a turn-scoped failure names
# its turn, and a session failure has nowhere to put one.
# This class is the retired spelling they join. An adapter that has not migrated
# still constructs it — `error` plus an optional `turn_id` — and a reader that has
# not migrated still filters for it with `isinstance`; both keep working, because
# the constructor keeps the old shape and it is the base of the two below. What
# changed is what `turn_id=None` means: nothing. It no longer reaches a turn, and
# a failure that names none can only end the session it came from. Each adapter
# moves to the two names below in its own change, and this class goes with the last.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
class SessionFaulted:
    def __init__(self, error: str, turn_id: str | None = None) -> None:
        self.error = error
        self.turn_id = turn_id


# meiva:note
# One turn failed, and this event names it, so only that turn can be ended by it.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
@dataclass(frozen=True, slots=True)
class TurnFaulted(SessionFaulted):
    turn_id: str
    error: str


# meiva:note
# The session itself failed. It cannot name a turn, and no reader may infer one.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
@dataclass(frozen=True, slots=True)
class SessionFailed(SessionFaulted):
    error: str


# meiva:note
# Provider output that belongs to no turn this session ran: a terminal message the
# provider produced on its own initiative — the CLI's own answer to a background
# task that finished while the agent was idle, a completion for a turn this session
# never started, a result the protocol cannot attribute. It is not a turn boundary
# and not a failure: it is content the agent has not necessarily seen, so the
# session hands it to its sink, which delivers it the way any other inbound is
# delivered. `source` is the provider's own name for it; `text` is what the agent
# should read.
# meiva:link base/lm/harness/docs/attribution.meiva.okf.md
@dataclass(frozen=True, slots=True)
class UnsolicitedOutput:
    source: str
    text: str


UnsolicitedSink = Callable[[UnsolicitedOutput], None]


BridgeEvent = (
    SessionStarted
    | SessionBound
    | HandoffInjected
    | ReasoningDelta
    | ReasoningText
    | TextDelta
    | AssistantText
    | ToolUse
    | ToolResult
    | RateLimitObserved
    | TurnCompleted
    | UnsolicitedOutput
    | SessionClosed
    | TurnFaulted
    | SessionFailed
    | SessionFaulted
)


# meiva:note
# `TextDelta` and `ReasoningDelta` alone are droppable: a consumer that falls behind
# may skip incremental stream deltas, but never a turn boundary, a tool result or a
# session binding, because losing one of those loses structure rather than detail.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
def is_droppable(event: object) -> bool:
    return isinstance(event, (TextDelta, ReasoningDelta))
