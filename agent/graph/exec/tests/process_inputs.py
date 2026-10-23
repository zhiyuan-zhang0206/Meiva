import asyncio
from collections.abc import Awaitable, Sequence
from dataclasses import dataclass
from typing import Any

from agent.graph.exec.node import ExecOutputChunkPublisher, ExecResult
from agent.graph.exec.protocol import ResultPayload
from base.agents.context import MeivaContext


@dataclass(frozen=True)
class ProcessCall:
    code: str
    context: MeivaContext
    cancel_event: asyncio.Event
    chunks: ExecOutputChunkPublisher
    state: dict[str, Any]
    config_overlay: dict[str, object]


class ScriptedExecProcess:
    def __init__(self, outcomes: Sequence[tuple[ExecResult, ResultPayload | None]]) -> None:
        self.outcomes = iter(outcomes)
        self.calls: list[ProcessCall] = []

    def run(
        self,
        code: str,
        context: MeivaContext,
        cancel_event: asyncio.Event,
        chunks: ExecOutputChunkPublisher,
        *,
        state: dict[str, Any],
        config_overlay: dict[str, object],
    ) -> Awaitable[tuple[ExecResult, ResultPayload | None]]:
        self.calls.append(ProcessCall(code, context, cancel_event, chunks, state, config_overlay))
        return self._resolve(next(self.outcomes))

    async def _resolve(
        self, outcome: tuple[ExecResult, ResultPayload | None]
    ) -> tuple[ExecResult, ResultPayload | None]:
        return outcome
