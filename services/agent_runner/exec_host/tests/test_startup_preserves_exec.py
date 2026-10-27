# meiva:note
# A live client owns its envelopes across either daemon's actual startup.
# Readiness and release files live outside run/exec so destructive startup cannot release the child.

from __future__ import annotations

import asyncio
import contextlib
import random
import sys
import tempfile
from collections.abc import AsyncGenerator, Callable, Generator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

import httpx
import pytest

from agent.execution.protocol import ResultPayload
from agent.graph.exec.process import ExecOutputChunkPublisher, ExecResult, SubprocessExecProcess
from agent.graph.exec.tests.process_inputs import live_parent_facts
from base.config.tests.configuration import defaults
from base.events.live.tests.live_capture import idle_publisher
from base.host.env.agent_slices import AgentSlices
from base.host.env.dotenv_file import upsert_env
from base.host.env.registry import env_keep_set
from base.paths import exec_run_dir, run_dir
from base.paths.tests.homes import point_home
from base.sessions.exec_host import protocol
from base.sessions.exec_host.paths import service_socket_path
from meiva.tests.fixtures.pin_agent import exec_context


# meiva:note
# A fresh seeded generator for each use, so no test sees another's draws.
def _rng() -> random.Random:
    return random.Random(0)  # noqa: S311


_ROOT = Path(__file__).resolve().parents[4]
_AGENT_ID = 424242


@pytest.fixture
def short_home(monkeypatch: pytest.MonkeyPatch) -> Generator[Path]:
    environ = live_parent_facts().environ
    with tempfile.TemporaryDirectory(prefix="exec-startup-", dir="/tmp") as name:
        home = Path(name)
        upsert_env(
            home / ".env", {key: environ[key] for key in env_keep_set("agent") & environ.keys()}
        )
        yield point_home(monkeypatch, home)


@dataclass
class ActiveExec:
    task: asyncio.Future[tuple[ExecResult, ResultPayload | None]]
    request: Path
    release: Path
    effect: Path


@contextlib.asynccontextmanager
async def _paused_exec(home: Path, directory: Path) -> AsyncGenerator[ActiveExec]:
    ready, release, effect = (directory / name for name in ("ready", "release", "effect"))
    code = (
        "from pathlib import Path\nimport time\n"
        f"Path({str(ready)!r}).write_text('ready')\n"
        "deadline = time.monotonic() + 60\n"
        f"while not Path({str(release)!r}).exists():\n"
        "    if time.monotonic() >= deadline:\n"
        "        raise TimeoutError('test release did not arrive')\n"
        "    time.sleep(0.01)\n"
        f"Path({str(effect)!r}).write_text('42')\n"
        "print('FINISHED')\n"
    )
    context = replace(
        exec_context(_AGENT_ID),
        agent=AgentSlices.resolve(default_reader=defaults(sandbox__exec_timeout_seconds=90)),
    )
    cancel = asyncio.Event()
    process = SubprocessExecProcess(live_parent_facts(), rng=_rng())
    task = asyncio.ensure_future(
        process.run(
            code,
            context,
            cancel,
            ExecOutputChunkPublisher(idle_publisher(), agent_id=_AGENT_ID, item_id="startup"),
            state={},
            config_overlay={},
        )
    )
    try:
        async with asyncio.timeout(30):
            while not ready.exists():
                if task.done():
                    raise AssertionError(f"exec exited before its barrier: {await task!r}")
                await asyncio.sleep(0.02)
        requests = list((exec_run_dir(home) / str(_AGENT_ID)).glob("req-*.json"))
        assert len(requests) == 1
        yield ActiveExec(task, requests[0], release, effect)
    finally:
        release.touch()
        if not task.done():
            cancel.set()
            _, pending = await asyncio.wait({task}, timeout=20)
            if pending:
                task.cancel()
                await asyncio.wait({task}, timeout=5)
        assert task.done(), "exec did not join after releasing and cancelling its real child"
        if not task.cancelled():
            task.exception()


@contextlib.asynccontextmanager
async def _daemon(
    kind: Literal["exec-host", "agent-host"], environ: Mapping[str, str], log: Path
) -> AsyncGenerator[asyncio.subprocess.Process]:
    if kind == "exec-host":
        command = (sys.executable, "-m", "services.agent_runner.exec_host.daemon")
    else:
        command = (sys.executable, "-m", "services.agent_runner.agent_host.daemon")
    with log.open("wb") as output:
        process = await asyncio.create_subprocess_exec(
            *command, cwd=_ROOT, env=environ, stdout=output, stderr=asyncio.subprocess.STDOUT
        )
        try:
            yield process
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 20)
                except TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), 5)


async def _ping(home: Path, process: asyncio.subprocess.Process, log: Path) -> None:
    socket = service_socket_path(run_dir(home))
    assert socket.parent == run_dir(home), (
        "this test must not use the shared short-socket directory"
    )
    async with asyncio.timeout(30):
        while True:
            assert process.returncode is None, log.read_text()
            try:
                reader, writer = await asyncio.open_unix_connection(str(socket))
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.02)
                continue
            try:
                writer.write(protocol.encode({"id": 1, "method": "ping"}))
                await writer.drain()
                reply = protocol.decode_object(await reader.readline())
                assert reply["ok"] is True and reply["data"]["pid"] == process.pid
                return
            finally:
                writer.close()
                await writer.wait_closed()


async def _health(port: int, home: Path, process: asyncio.subprocess.Process, log: Path) -> None:
    async with asyncio.timeout(45), httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
        while True:
            assert process.returncode is None, log.read_text()
            try:
                response = await client.get(f"http://127.0.0.1:{port}/healthz")
            except httpx.TransportError:
                await asyncio.sleep(0.02)
                continue
            response.raise_for_status()
            body = response.json()
            assert body["pid"] == process.pid and body["home"] == str(home)
            assert body["name"] == "agent_host"
            return


async def _assert_completed(active: ActiveExec) -> None:
    request_preserved = active.request.exists()
    active.release.touch()
    done, _ = await asyncio.wait({active.task}, timeout=20)
    assert done, "released exec did not complete"
    result, payload = await active.task
    assert active.effect.read_text() == "42"
    assert payload is not None and payload.kind == "done", (result, request_preserved)
    assert request_preserved
    assert "FINISHED" in result.output
    assert not active.request.exists()
    assert not list(active.request.parent.glob("*.json"))


@pytest.mark.parametrize("start_daemon", [False, True], ids=["control", "exec-host-startup"])
async def test_exec_host_startup_preserves_an_active_direct_child(
    short_home: Path, tmp_path: Path, start_daemon: bool
) -> None:
    socket = service_socket_path(run_dir(short_home))
    assert not socket.exists()
    environ = live_parent_facts().environ
    log = tmp_path / "exec-host.log"
    async with asyncio.timeout(90), _paused_exec(short_home, tmp_path) as active:
        if start_daemon:
            async with _daemon("exec-host", environ, log) as process:
                await _ping(short_home, process, log)
                await _assert_completed(active)
        else:
            await _assert_completed(active)


async def test_agent_host_startup_preserves_an_active_delegated_child(
    short_home: Path,
    tmp_path: Path,
    db_url: str,
    redis_url: str,
    distinct_free_ports: Callable[[int], list[int]],
) -> None:
    port = distinct_free_ports(1)[0]
    environ = dict(live_parent_facts().environ) | {
        "MEIVA_DB_URL": db_url,
        "MEIVA_REDIS_URL": redis_url,
        "MEIVA_MACHINE_NAME": "startup-sweep-test",
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
        "MEIVA_AGENT_HOST_HEALTH_PORT": str(port),
    }
    environ.pop("MEIVA_PERMISSIONS_HELPER_PID", None)
    upsert_env(
        short_home / ".env",
        {key: environ[key] for key in env_keep_set("agent") & environ.keys()},
    )
    exec_log, host_log = tmp_path / "exec-host.log", tmp_path / "agent-host.log"
    async with asyncio.timeout(120), _daemon("exec-host", environ, exec_log) as service:
        await _ping(short_home, service, exec_log)
        async with (
            _paused_exec(short_home, tmp_path) as active,
            _daemon("agent-host", environ, host_log) as host,
        ):
            await _health(port, short_home, host, host_log)
            await _assert_completed(active)
