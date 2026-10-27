import asyncio
import random
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from agent.execution.protocol import ResultPayload
from base.agents.context import MeivaContext
from base.paths import exec_run_dir

from ._child_boundary import ParentFacts, result_from_payload
from ._result import ExecCrashed, ExecDone, ExecResult
from ._stream import ExecOutputChunkPublisher
from ._subprocess import run_in_subprocess

__all__ = [
    "ExecCrashed",
    "ExecDone",
    "ExecProcess",
    "ExecResult",
    "ParentFacts",
    "SubprocessExecProcess",
    "result_from_payload",
    "run_in_subprocess",
]


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


# meiva:note
# The production exec process: one disposable child per call, launched from the parent facts it
# was constructed with. Each call draws the identities of its request and result envelopes from
# `rng`, which the composition root builds (a CSPRNG in production).
@dataclass(frozen=True)
class SubprocessExecProcess:
    parent: ParentFacts
    rng: random.Random

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
            parent=self.parent,
            exec_dir=exec_run_dir(context.require_meiva_home()),
            request_id=UUID(int=self.rng.getrandbits(128), version=4),
            result_id=UUID(int=self.rng.getrandbits(128), version=4),
            state=state,
            config_overlay=config_overlay,
        )
