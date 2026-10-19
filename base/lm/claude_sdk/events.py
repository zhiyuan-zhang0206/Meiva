from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


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


@dataclass(frozen=True, slots=True)
class SessionClosed:
    reason: str
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class SessionFaulted:
    error: str
    turn_id: str | None = None


BridgeEvent = (
    SessionStarted
    | SessionBound
    | HandoffInjected
    | TextDelta
    | AssistantText
    | ToolUse
    | ToolResult
    | RateLimitObserved
    | TurnCompleted
    | SessionClosed
    | SessionFaulted
)


# meiva:note
# `TextDelta` alone is droppable: a consumer that falls behind may skip text, but
# never a turn boundary, a tool result or a session binding, because losing one of
# those loses structure rather than detail.
# meiva:link base/lm/claude_sdk/docs/claude_sdk.meiva.okf.md
def is_droppable(event: object) -> bool:
    return isinstance(event, TextDelta)
