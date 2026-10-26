"""The execution owner's complete, unsampled public SDK-call counts."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Lock

from pydantic import BaseModel

from base.agents.messages.kwargs import MeivaMsgType, read_meiva_kwargs


# meiva:note
# Per-method SDK call count recorded on an agent_code block's exec_output ToolMessage.
class SdkCall(BaseModel):
    method: str
    count: int


# meiva:note
# Extracts recorded runtime SDK call tallies per tool_call_id from exec_output ToolMessages in messages[start:].
def sdk_calls_by_tool_call_id(
    messages: Sequence[object], start: int = 0
) -> dict[str, list[SdkCall]]:
    from langchain_core.messages import ToolMessage

    out: dict[str, list[SdkCall]] = {}
    for msg in messages[start:]:
        if not isinstance(msg, ToolMessage):
            continue
        kwargs = read_meiva_kwargs(msg)
        if kwargs.get("meiva_msg_type") != MeivaMsgType.EXEC_OUTPUT:
            continue
        calls = kwargs.get("sdk_calls")
        if calls is None:
            continue
        out[msg.tool_call_id] = [SdkCall.model_validate(entry) for entry in calls]
    return out


@dataclass(frozen=True)
class SdkCallTally:
    """Shared by calls and threads belonging to one explicit execution context."""

    _counts: dict[str, int] = field(default_factory=dict[str, int], init=False, repr=False)
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def add(self, fn: str) -> None:
        """Count one admitted invocation, including a body that failed."""
        with self._lock:
            self._counts[fn] = self._counts.get(fn, 0) + 1

    def snapshot(self) -> dict[str, int]:
        """Copy completed call counts while other execution threads may still run."""
        with self._lock:
            return dict(self._counts)
