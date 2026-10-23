import asyncio
from collections.abc import Awaitable
from typing import Any, Protocol

from agent.execution.protocol import ResultPayload
from base.agents.context import MeivaContext
from base.paths import exec_run_dir, meiva_home

from ._child_boundary import ParentFacts, read_parent_facts
from ._result import ExecResult
from ._stream import ExecOutputChunkPublisher
from ._subprocess import run_in_subprocess


class ExecProcess(Protocol):
    def run(
        self,
        code: str,
        context: MeivaContext,
        cancel_event: asyncio.Event,
        chunks: ExecOutputChunkPublisher,
        *,
        state: dict[str, Any],
        config_overlay: dict[str, object],
    ) -> Awaitable[tuple[ExecResult, ResultPayload | None]]: ...


class SubprocessExecProcess:
    def parent_facts(self) -> ParentFacts:
        return read_parent_facts()

    # meiva:note
    # Capture inner limits before the caller's shield reads its outer timeout.
    # An async method body would defer these reads until after task scheduling.
    # meiva:link agent/graph/exec/docs/process.meiva.okf.md
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
        return run_in_subprocess(
            code,
            context,
            cancel_event,
            context.require_agent().read("sandbox", "exec_timeout_seconds"),
            chunks,
            accumulation_max_chars=context.require_agent().read(
                "sandbox", "exec_output_accumulation_max_chars"
            ),
            parent=self.parent_facts(),
            exec_dir=exec_run_dir(meiva_home()),
            state=state,
            config_overlay=config_overlay,
        )
