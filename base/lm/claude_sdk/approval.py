from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from base.lm.claude_sdk.events import ApprovalDecided, ApprovalRequested


@dataclass(frozen=True, slots=True)
class ApprovalPolicyResult:
    allow: bool
    message: str | None = None


ApprovalPolicy = Callable[[str, Mapping[str, Any]], ApprovalPolicyResult | None]


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    allow: bool
    source: str
    message: str | None
    latency_ms: float


@dataclass(slots=True)
class PendingApproval:
    approval_id: str
    turn_id: str
    tool_name: str
    tool_input: dict[str, Any]
    tool_use_id: str | None
    requested_at: float
    future: asyncio.Future[tuple[bool, str | None, str]] = field(repr=False)


class ApprovalBroker:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        emit: Callable[[Any], None],
        policy: ApprovalPolicy | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._emit = emit
        self._policy = policy
        self._clock = clock
        self._pending: dict[str, PendingApproval] = {}

    @property
    def pending(self) -> Mapping[str, PendingApproval]:
        return MappingProxyType(self._pending)

    async def request(
        self,
        *,
        turn_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_use_id: str | None,
        title: str | None = None,
        description: str | None = None,
    ) -> ApprovalOutcome:
        started = self._clock()
        policy_result = self._apply_policy(tool_name, tool_input)
        if policy_result is not None:
            return self._settle(
                turn_id=turn_id,
                approval_id=tool_use_id or f"approval-{uuid4().hex}",
                allow=policy_result.allow,
                source="policy",
                message=policy_result.message,
                started=started,
            )
        approval_id = tool_use_id or f"approval-{uuid4().hex}"
        future: asyncio.Future[tuple[bool, str | None, str]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[approval_id] = PendingApproval(
            approval_id=approval_id,
            turn_id=turn_id,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_use_id=tool_use_id,
            requested_at=started,
            future=future,
        )
        self._emit(
            ApprovalRequested(
                turn_id=turn_id,
                approval_id=approval_id,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=tool_use_id,
                title=title,
                description=description,
            )
        )
        try:
            async with asyncio.timeout(self._timeout_seconds):
                allow, message, source = await future
        except TimeoutError:
            self._pending.pop(approval_id, None)
            return self._settle(
                turn_id=turn_id,
                approval_id=approval_id,
                allow=False,
                source="timeout",
                message=f"No approval decision within {self._timeout_seconds:.0f}s; denied by default.",
                started=started,
            )
        return self._settle(
            turn_id=turn_id,
            approval_id=approval_id,
            allow=allow,
            source=source,
            message=message,
            started=started,
        )

    def resolve(self, approval_id: str, *, allow: bool, message: str | None = None) -> bool:
        pending = self._pending.pop(approval_id, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result((allow, message, "human"))
        return True

    def deny_all(self, reason: str) -> int:
        approvals = list(self._pending.values())
        self._pending.clear()
        for pending in approvals:
            if not pending.future.done():
                pending.future.set_result((False, reason, "closed"))
        return len(approvals)

    def _apply_policy(
        self, tool_name: str, tool_input: Mapping[str, Any]
    ) -> ApprovalPolicyResult | None:
        if self._policy is None:
            return None
        return self._policy(tool_name, tool_input)

    def _settle(
        self,
        *,
        turn_id: str,
        approval_id: str,
        allow: bool,
        source: str,
        message: str | None,
        started: float,
    ) -> ApprovalOutcome:
        latency_ms = (self._clock() - started) * 1000.0
        self._emit(
            ApprovalDecided(
                turn_id=turn_id,
                approval_id=approval_id,
                allow=allow,
                source=source,
                message=message,
                latency_ms=latency_ms,
            )
        )
        return ApprovalOutcome(allow=allow, source=source, message=message, latency_ms=latency_ms)
