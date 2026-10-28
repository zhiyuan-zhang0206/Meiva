# meiva:note
# The public exec process runs real children with and without a real exec-host socket.
# Both runners use this interpreter's source checkout; only the launch owner changes.

from __future__ import annotations

import asyncio
import json
import random
import tempfile
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Literal, assert_never

import pytest

from agent.execution.protocol import ResultPayload
from agent.graph.exec.process import ExecOutputChunkPublisher, ExecResult, SubprocessExecProcess
from agent.graph.exec.tests.process_inputs import live_parent_facts
from base.clock import SYSTEM_TIMERS
from base.config.tests.configuration import defaults
from base.events.live.tests.live_capture import idle_publisher
from base.host.env.agent_env import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV
from base.host.env.agent_slices import AgentSlices
from base.paths import run_dir
from base.sessions.exec_host.paths import service_socket_path
from meiva.tests.fixtures.pin_agent import exec_context
from services.agent_runner.exec_host.service import ExecHostService


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


type Location = Literal["inside", "outside", "worktree", "claude-worktree"]

_OBSERVE_ENV = """
import json, os, sys
from meiva.sdk_surface.settings import agent_setting
keys = (
    "VIRTUAL_ENV", "MEIVA_PERMISSIONS_HELPER_PID", "MEIVA_PROCESS_PROFILE",
    "MEIVA_AGENT_ID", "MEIVA_AGENT_CONFIG_OVERLAY", "MEIVA_AGENT_BIRTH_CONFIG",
)
print("CHILD_ENV=" + json.dumps({key: os.environ.get(key) for key in keys}, sort_keys=True))
print("CHILD_PYTHON=" + sys.executable)
print("CHILD_WORD_LIMIT=" + str(agent_setting("to_user_soft_limit_words")))
"""


def _observe_child_environment(
    result: ExecResult,
    payload: ResultPayload | None,
    executable: str,
    word_limit: float,
) -> dict[str, str | None]:
    assert payload is not None and payload.kind == "done", result
    assert result.output.split("CHILD_PYTHON=", 1)[1].splitlines()[0] == executable
    assert float(result.output.split("CHILD_WORD_LIMIT=", 1)[1].splitlines()[0]) == word_limit
    environ: dict[str, str | None] = json.loads(
        result.output.split("CHILD_ENV=", 1)[1].splitlines()[0]
    )
    return environ


@contextmanager
def _child_cwd(source_root: Path, outside: Path, location: Location) -> Generator[Path]:
    match location:
        case "inside":
            yield source_root / "agent"
        case "outside":
            yield outside
        case "worktree" | "claude-worktree":
            nested = (
                source_root / ".worktrees"
                if location == "worktree"
                else source_root / ".claude" / "worktrees"
            )
            nested.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="exec-env-test-", dir=nested) as directory:
                yield Path(directory)
        case _:
            assert_never(location)


@asynccontextmanager
async def _serving(service: ExecHostService, socket_path: Path) -> AsyncGenerator[None]:
    async with asyncio.TaskGroup() as tasks:
        task = tasks.create_task(service.serve(socket_path, install_signals=False))
        try:
            deadline = time.monotonic() + 5.0
            while not socket_path.exists():
                if task.done():
                    await task
                    raise AssertionError("exec-host stopped before becoming ready")
                assert time.monotonic() < deadline, "exec-host did not become ready"
                await asyncio.sleep(0.01)
            yield
        finally:
            service.request_stop()


@pytest.mark.parametrize("location", ["inside", "outside", "worktree", "claude-worktree"])
@pytest.mark.parametrize("config_overlay", [{}, {"to_user_soft_limit_words": 30}])
async def test_socket_and_direct_children_observe_the_same_checkout_environment(
    unit_home: Path,
    tmp_path: Path,
    location: Location,
    config_overlay: dict[str, object],
) -> None:
    parent = live_parent_facts()
    source_root = parent.source_root
    assert source_root is not None
    context = replace(exec_context(424242), agent=AgentSlices.resolve(default_reader=defaults()))
    default_word_limit = context.require_agent().read("agent", "to_user_soft_limit_words")
    venv = str(Path(parent.executable).parent.parent)
    parent = replace(
        parent,
        environ={
            **parent.environ,
            "VIRTUAL_ENV": venv,
            "MEIVA_PERMISSIONS_HELPER_PID": "100001",
            AGENT_CONFIG_OVERLAY_ENV: json.dumps(
                {"to_user_soft_limit_words": default_word_limit + 1}
            ),
            AGENT_BIRTH_CONFIG_ENV: json.dumps(
                {"to_user_soft_limit_words": default_word_limit + 2}
            ),
        },
    )
    service = ExecHostService(
        home=unit_home,
        base_environ={
            **parent.environ,
            "MEIVA_PERMISSIONS_HELPER_PID": "100002",
            "VIRTUAL_ENV": venv,
        },
        default_cwd=unit_home,
        source_root=source_root,
        timers=SYSTEM_TIMERS,
        executable=parent.executable,
    )
    chunks = ExecOutputChunkPublisher(idle_publisher(), agent_id=424242, item_id="env")
    socket_path = service_socket_path(run_dir(unit_home))

    with _child_cwd(source_root, tmp_path, location) as cwd:
        process = SubprocessExecProcess(replace(parent, cwd=cwd), rng=_rng())
        assert not socket_path.exists()
        direct, direct_payload = await process.run(
            _OBSERVE_ENV,
            context,
            asyncio.Event(),
            chunks,
            state={},
            config_overlay=config_overlay,
        )
        async with _serving(service, socket_path):
            remote, remote_payload = await process.run(
                _OBSERVE_ENV,
                context,
                asyncio.Event(),
                chunks,
                state={},
                config_overlay=config_overlay,
            )

    word_limit = 30 if config_overlay else default_word_limit
    direct_env = _observe_child_environment(direct, direct_payload, parent.executable, word_limit)
    remote_env = _observe_child_environment(remote, remote_payload, parent.executable, word_limit)
    assert direct_env.pop("MEIVA_PERMISSIONS_HELPER_PID") == "100001"
    assert remote_env.pop("MEIVA_PERMISSIONS_HELPER_PID") == "100002"
    assert direct_env == remote_env
    assert direct_env["VIRTUAL_ENV"] == (venv if location == "inside" else None)
    assert direct_env[AGENT_CONFIG_OVERLAY_ENV] is None
    assert direct_env[AGENT_BIRTH_CONFIG_ENV] is None
    assert direct_env["MEIVA_AGENT_ID"] == "424242"
    assert direct_env["MEIVA_PROCESS_PROFILE"] == "agent"
