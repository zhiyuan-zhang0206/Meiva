from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from base.clock import SYSTEM_TIMERS
from base.config.tests.configuration import defaults
from base.deploy.release import editable_install
from base.host.env.agent_env import AGENT_BIRTH_CONFIG_ENV, AGENT_CONFIG_OVERLAY_ENV
from base.native_process import child_env
from base.native_process.exec_kill_notice import read_notice, write_notice
from base.native_process.os_platform import file_lock
from base.paths import exec_run_dir, run_dir
from base.sessions.exec_host import protocol
from base.sessions.exec_host.paths import lock_path, service_socket_path
from services.agent_runner.exec_host import daemon, memory_guard
from services.agent_runner.exec_host.service import ExecHostService


@pytest.mark.parametrize(
    ("birth_config", "config_overlay"),
    [
        (None, None),
        ({}, {}),
        ({"to_user_soft_limit_words": 30}, None),
        (None, {"to_user_soft_limit_words": 30}),
    ],
)
async def test_run_consumes_only_explicit_config_maps(
    tmp_path: Path,
    birth_config: dict[str, object] | None,
    config_overlay: dict[str, object] | None,
) -> None:
    default_word_limit = defaults()("agent", "to_user_soft_limit_words")
    request_path = exec_run_dir(tmp_path) / "1" / "req-config.json"
    result_path = request_path.with_name("res-config.json")
    _write_request_envelope(
        request_path,
        "import json, os\n"
        "from meiva.sdk_surface.settings import agent_setting\n"
        "print('CHILD_CONFIG=' + json.dumps({\n"
        "    'overlay': os.environ.get('MEIVA_AGENT_CONFIG_OVERLAY'),\n"
        "    'birth': os.environ.get('MEIVA_AGENT_BIRTH_CONFIG'),\n"
        "    'word_limit': agent_setting('to_user_soft_limit_words'),\n"
        "}))\n",
    )
    async with _running_service(tmp_path) as sock_path:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            writer.write(
                protocol.encode(
                    {
                        "id": 1,
                        "method": "run",
                        "agent_id": 1,
                        "request_path": str(request_path),
                        "result_path": str(result_path),
                        "timeout_s": 15.0,
                        "env_overlay": {
                            AGENT_CONFIG_OVERLAY_ENV: json.dumps(
                                {"to_user_soft_limit_words": default_word_limit + 1}
                            ),
                            AGENT_BIRTH_CONFIG_ENV: json.dumps(
                                {"to_user_soft_limit_words": default_word_limit + 2}
                            ),
                        },
                        "config_overlay": config_overlay,
                        "birth_config": birth_config,
                    }
                )
            )
            await writer.drain()
            frames = await _read_frames(reader)
        finally:
            writer.close()
            await writer.wait_closed()

    assert frames[-1]["ok"] is True
    assert frames[-1]["data"]["returncode"] == 0
    output = "".join(frame["data"] for frame in frames if frame.get("type") == "output")
    observed = json.loads(output.split("CHILD_CONFIG=", 1)[1].splitlines()[0])
    assert observed == {
        "overlay": None,
        "birth": None,
        "word_limit": 30 if birth_config or config_overlay else default_word_limit,
    }


def _write_request_envelope(
    path: Path, code: str, *, agent_id: int = 1, timeout_s: float = 10.0
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "v": 1,
        "code": code,
        "agent_id": agent_id,
        "context": {
            "identity": {"agent_id": agent_id, "owns_loop": True, "actor": None},
            "gateway_url": "http://127.0.0.1:8000",
        },
        "timeout_s": timeout_s,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@contextlib.asynccontextmanager
async def _running_service(home: Path) -> AsyncGenerator[Path]:
    sock_path = service_socket_path(run_dir(home))
    base_env = {
        **child_env.inherited_process_env(),
        "MEIVA_HOME": str(home),
        "MEIVA_CONFIG_FETCH": "skip",
    }
    service = ExecHostService(
        home=home,
        base_environ=base_env,
        default_cwd=home,
        source_root=editable_install.current_interpreter_source_root(sys.executable),
        timers=SYSTEM_TIMERS,
        pid=os.getpid(),
    )
    async with asyncio.TaskGroup() as tg:
        tg.create_task(service.serve(sock_path, install_signals=False))
        deadline = time.monotonic() + 5.0
        while not sock_path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("exec-host socket did not appear")
            await asyncio.sleep(0.01)
        try:
            yield sock_path
        finally:
            service.request_stop()


async def _read_frames(reader: asyncio.StreamReader) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    while True:
        line = await reader.readline()
        if not line:
            break
        msg = protocol.decode_object(line)
        frames.append(msg)
        if "ok" in msg:
            break
    return frames


@pytest.mark.asyncio
async def test_ping_returns_ok_and_pid(tmp_path: Path) -> None:
    async with _running_service(tmp_path) as sock_path:
        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            writer.write(protocol.encode({"id": 1, "method": "ping"}))
            await writer.drain()
            line = await reader.readline()
            resp = protocol.decode_object(line)
            assert resp["id"] == 1
            assert resp["ok"] is True
            assert resp["code"] == protocol.OK
            assert resp["data"] == {"pid": os.getpid()}
        finally:
            writer.close()
            await writer.wait_closed()


@pytest.mark.asyncio
async def test_run_streams_output_and_writes_result(tmp_path: Path) -> None:
    async with _running_service(tmp_path) as sock_path:
        exec_dir = exec_run_dir(tmp_path)
        req_file = exec_dir / "1" / "req-1.json"
        res_file = exec_dir / "1" / "res-1.json"
        _write_request_envelope(req_file, "print('hello exec-host', flush=True)\n")

        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            writer.write(
                protocol.encode(
                    {
                        "id": 2,
                        "method": "run",
                        "agent_id": 1,
                        "request_path": str(req_file),
                        "result_path": str(res_file),
                        "timeout_s": 15.0,
                        "cwd": str(tmp_path),
                    }
                )
            )
            await writer.drain()
            frames = await _read_frames(reader)
        finally:
            writer.close()
            await writer.wait_closed()

        output_text = "".join(f["data"] for f in frames if f.get("type") == "output")
        assert "hello exec-host" in output_text
        final = frames[-1]
        assert final["ok"] is True
        assert final["data"] == {"returncode": 0, "cancelled": False, "timed_out": False}
        assert res_file.exists()
        envelope = json.loads(res_file.read_text(encoding="utf-8"))
        assert envelope["kind"] == "done"


@pytest.mark.asyncio
async def test_cancel_mid_run_kills_process_group(tmp_path: Path) -> None:
    async with _running_service(tmp_path) as sock_path:
        exec_dir = exec_run_dir(tmp_path)
        req_file = exec_dir / "1" / "req-cancel.json"
        res_file = exec_dir / "1" / "res-cancel.json"
        pid_file = tmp_path / "bg.pid"
        code = (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"Path({str(pid_file)!r}).write_text(str(p.pid))\n"
            "print('READY', flush=True)\n"
            "time.sleep(60)\n"
        )
        _write_request_envelope(req_file, code, timeout_s=60.0)

        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            writer.write(
                protocol.encode(
                    {
                        "id": 3,
                        "method": "run",
                        "agent_id": 1,
                        "request_path": str(req_file),
                        "result_path": str(res_file),
                        "timeout_s": 60.0,
                        "cwd": str(tmp_path),
                    }
                )
            )
            await writer.drain()

            while True:
                line = await reader.readline()
                assert line
                msg = protocol.decode_object(line)
                if msg.get("type") == "output" and "READY" in msg.get("data", ""):
                    break

            bg_pid = int(pid_file.read_text(encoding="utf-8").strip())
            writer.write(protocol.encode({"id": 3, "method": "cancel"}))
            await writer.drain()

            frames = await _read_frames(reader)
        finally:
            writer.close()
            await writer.wait_closed()

        final = frames[-1]
        assert final["ok"] is True
        assert final["data"]["cancelled"] is True
        assert final["data"]["timed_out"] is False
        with pytest.raises(ProcessLookupError):
            os.kill(bg_pid, 0)


@pytest.mark.asyncio
async def test_timeout_kills_process_group(tmp_path: Path) -> None:
    async with _running_service(tmp_path) as sock_path:
        exec_dir = exec_run_dir(tmp_path)
        req_file = exec_dir / "1" / "req-timeout.json"
        res_file = exec_dir / "1" / "res-timeout.json"
        pid_file = tmp_path / "bg-timeout.pid"
        code = (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"Path({str(pid_file)!r}).write_text(str(p.pid))\n"
            "time.sleep(60)\n"
        )
        _write_request_envelope(req_file, code, timeout_s=1.5)

        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        try:
            writer.write(
                protocol.encode(
                    {
                        "id": 4,
                        "method": "run",
                        "agent_id": 1,
                        "request_path": str(req_file),
                        "result_path": str(res_file),
                        "timeout_s": 1.5,
                        "cwd": str(tmp_path),
                    }
                )
            )
            await writer.drain()
            frames = await _read_frames(reader)
        finally:
            writer.close()
            await writer.wait_closed()

        final = frames[-1]
        assert final["ok"] is True
        assert final["data"]["cancelled"] is False
        assert final["data"]["timed_out"] is True
        bg_pid = int(pid_file.read_text(encoding="utf-8").strip())
        with pytest.raises(ProcessLookupError):
            os.kill(bg_pid, 0)


@pytest.mark.asyncio
async def test_client_disconnect_kills_process_group(tmp_path: Path) -> None:
    async with _running_service(tmp_path) as sock_path:
        exec_dir = exec_run_dir(tmp_path)
        req_file = exec_dir / "1" / "req-disc.json"
        res_file = exec_dir / "1" / "res-disc.json"
        pid_file = tmp_path / "bg-disc.pid"
        code = (
            "import subprocess, sys, time\n"
            "from pathlib import Path\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            f"Path({str(pid_file)!r}).write_text(str(p.pid))\n"
            "print('STARTED', flush=True)\n"
            "time.sleep(60)\n"
        )
        _write_request_envelope(req_file, code, timeout_s=60.0)

        reader, writer = await asyncio.open_unix_connection(str(sock_path))
        writer.write(
            protocol.encode(
                {
                    "id": 5,
                    "method": "run",
                    "agent_id": 1,
                    "request_path": str(req_file),
                    "result_path": str(res_file),
                    "timeout_s": 60.0,
                    "cwd": str(tmp_path),
                }
            )
        )
        await writer.drain()

        while True:
            line = await reader.readline()
            assert line
            msg = protocol.decode_object(line)
            if msg.get("type") == "output" and "STARTED" in msg.get("data", ""):
                break

        bg_pid = int(pid_file.read_text(encoding="utf-8").strip())
        writer.close()
        await writer.wait_closed()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                os.kill(bg_pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.05)
        with pytest.raises(ProcessLookupError):
            os.kill(bg_pid, 0)


def test_duplicate_daemon_startup_fails_fast_with_lock(tmp_path: Path) -> None:
    run = run_dir(tmp_path)
    run.mkdir(parents=True, exist_ok=True)
    with file_lock(lock_path(run), timeout_s=0):
        assert daemon.run_with_home(tmp_path, default_cwd=tmp_path) == 1


def test_memory_guard_selects_largest_domain_and_writes_notice(tmp_path: Path) -> None:
    res_small = tmp_path / "res-small.json"
    res_big = tmp_path / "res-big.json"
    small = memory_guard.ExecDomain(
        pid=100, create_time=1.0, agent_id=1, result_path=res_small, footprint=1024**3
    )
    big = memory_guard.ExecDomain(
        pid=200, create_time=1.0, agent_id=2, result_path=res_big, footprint=4 * 1024**3
    )
    assert memory_guard.select_victim("normal", [small, big]) is None
    chosen = memory_guard.select_victim("critical", [small, big])
    assert chosen is not None
    victim, notice = chosen
    assert victim == big
    write_notice(victim.result_path, notice)
    assert read_notice(res_big) == notice
    assert "4.0 GiB" in notice
