import asyncio
import os
import sys
from collections.abc import Awaitable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent.execution.protocol import ResultPayload
from base.agents.context import MeivaContext
from base.deploy.release import editable_install
from base.paths import exec_run_dir, meiva_home

from ._child_boundary import ParentFacts
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


# meiva:note
# What this process looks like at the moment it spawns a child: the environment the child
# inherits, its working directory, the interpreter it re-executes, the checkout that
# interpreter belongs to and the one extra checkout its editable install may name. The
# composition root reads it once and hands it to `SubprocessExecProcess`.
def read_parent_facts() -> ParentFacts:
    return ParentFacts(
        environ=dict(os.environ),
        cwd=Path.cwd().resolve(),
        executable=sys.executable,
        source_root=editable_install.current_interpreter_source_root(),
        editable_roots=(Path.home() / "Meiva",),
    )


# meiva:note
# The production exec process: one disposable child per call, launched from the parent facts it
# was constructed with.
@dataclass(frozen=True)
class SubprocessExecProcess:
    parent: ParentFacts

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
            exec_dir=exec_run_dir(meiva_home()),
            state=state,
            config_overlay=config_overlay,
        )
