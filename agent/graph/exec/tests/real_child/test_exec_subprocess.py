from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import psutil
import pytest
from langchain_core.messages import HumanMessage

from agent.execution.protocol import STALE_FILE_AGE_S
from agent.graph.exec._child_boundary import ParentFacts
from agent.graph.exec._subprocess import run_in_subprocess
from agent.graph.exec.node import (
    ExecCancelled,
    ExecChildError,
    ExecCrashed,
    ExecDone,
    ExecOutputChunkPublisher,
    ExecTimedOut,
)
from agent.graph.exec.tests.process_inputs import real_parent
from base.clock import Clock, ClockConfig, Timers
from base.host.env.dotenv_file import upsert_env
from base.host.proc import kill_process_tree
from base.native_process.exec_domain import ExecProcessDomain
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from base.paths import logs_dir, resolve_meiva_home
from meiva.tests.fixtures.pin_agent import exec_context

AGENT_ID = 424242
_SLEEP_CHILD = Path(__file__).with_name("sleep_child.py")


# meiva:note
# Advances monotonic time past the exec deadline only after `ready()` observes the
# child running, so timeout tests never depend on wall-clock import speed or sleep.
# meiva:link agent/graph/exec/docs/subprocess-tests.meiva.okf.md
class _ExpireAfterReadyTimers:
    def __init__(self, ready: Callable[[], bool], *, advance_s: float) -> None:
        self._ready = ready
        self._advance_s = advance_s
        self._offset = 0.0

    def monotonic(self) -> float:
        now = time.monotonic()
        if self._offset == 0.0 and self._ready():
            self._offset = self._advance_s
        return now + self._offset

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def asleep(self, seconds: float) -> Coroutine[object, object, None]:
        return asyncio.sleep(seconds)


async def run_exec_child(
    tmp_path: Path,
    code: str,
    *,
    timeout: float = 30.0,
    cancel_after: float | None = None,
    state: dict[str, Any] | None = None,
    chunk_publisher: ExecOutputChunkPublisher | None = None,
    parent: ParentFacts | None = None,
    timers: Timers | None = None,
):
    cancel_event = asyncio.Event()
    cancel_task: asyncio.Task[None] | None = None
    if cancel_after is not None:

        async def _set_cancel() -> None:
            await asyncio.sleep(cancel_after)
            cancel_event.set()

        cancel_task = asyncio.create_task(_set_cancel())
    context = exec_context(AGENT_ID)
    if timers is not None:
        clock_cfg = ClockConfig("UTC", None, message_timestamp_weekday=False)
        context = replace(context, clock_factory=lambda: Clock(clock_cfg, timers=timers))
    try:
        result, _payload = await run_in_subprocess(
            code,
            context,
            cancel_event,
            timeout,
            chunk_publisher,
            state=state,
            exec_dir=tmp_path / "exec",
            parent=parent or real_parent(),
            accumulation_max_chars=1_000_000,
            request_id=UUID(int=1),
            result_id=UUID(int=2),
        )
        return result
    finally:
        if cancel_task is not None:
            cancel_task.cancel()


async def _assert_tree_gone(pids: list[int], timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    remaining = set(pids)
    while remaining and time.monotonic() < deadline:
        for pid in list(remaining):
            try:
                if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                    remaining.discard(pid)
            except psutil.NoSuchProcess:
                remaining.discard(pid)
        if remaining:
            await asyncio.sleep(0.05)
    assert not remaining, f"process(es) still alive after exec teardown: {sorted(remaining)}"


async def test_assert_tree_gone_accepts_missing_pid_immediately() -> None:
    await asyncio.wait_for(_assert_tree_gone([999_999_999]), timeout=0.5)


async def test_assert_tree_gone_waits_for_live_pid_to_become_zombie() -> None:
    sleeper, domain = ExecProcessDomain.launch_posix(
        [sys.executable, str(_SLEEP_CHILD), "0.3"], process_control=SYSTEM_PROCESS_CONTROL
    )
    try:
        await _assert_tree_gone([sleeper.pid], timeout_s=10.0)
    finally:
        domain.kill_and_reap()


async def test_assert_tree_gone_rejects_pid_still_running_after_deadline() -> None:
    sleeper, domain = ExecProcessDomain.launch_posix(
        [sys.executable, str(_SLEEP_CHILD), "60"], process_control=SYSTEM_PROCESS_CONTROL
    )
    try:
        with pytest.raises(AssertionError, match="still alive after exec teardown"):
            await _assert_tree_gone([sleeper.pid], timeout_s=0.3)
    finally:
        domain.kill_and_reap()


async def test_subprocess_done(tmp_path: Path) -> None:
    result = await run_exec_child(tmp_path, "print('hello from child')")
    assert isinstance(result, ExecDone)
    assert "hello from child" in result.output
    assert not list((tmp_path / "exec" / str(AGENT_ID)).iterdir())


async def test_a_run_prunes_the_stale_results_of_its_own_agent(tmp_path: Path) -> None:
    agent_dir = tmp_path / "exec" / str(AGENT_ID)
    agent_dir.mkdir(parents=True)
    stale = agent_dir / "res-left-behind.json"
    stale.write_text("{}", encoding="utf-8")
    long_ago = time.time() - STALE_FILE_AGE_S - 60
    os.utime(stale, (long_ago, long_ago))
    kept_request = agent_dir / "req-left-behind.json"
    kept_request.write_text("{}", encoding="utf-8")
    os.utime(kept_request, (long_ago, long_ago))

    await run_exec_child(tmp_path, "pass")

    assert not stale.exists()
    assert kept_request.exists()


async def test_exec_child_disables_otlp_after_cluster_env_authority(tmp_path: Path) -> None:
    env_path = Path(os.environ["MEIVA_HOME"]) / ".env"
    original_env = env_path.read_text() if env_path.exists() else None
    log_path = logs_dir(home=resolve_meiva_home()) / f"agent-{AGENT_ID}.log"
    log_offset = log_path.stat().st_size if log_path.exists() else 0
    upsert_env(env_path, {"MEIVA_TELEMETRY_OTLP_ENABLED": "true"})
    try:
        result = await run_exec_child(
            tmp_path,
            (
                "import meiva\n"
                "from base.telemetry.otlp import telemetry_otlp\n"
                "probes = []\n"
                "def endpoint_probe(endpoint):\n"
                "    probes.append(endpoint)\n"
                "    return False\n"
                "telemetry_otlp.endpoint_reachable = endpoint_probe\n"
                "meiva.context.clients.event_pipeline().warmup_exports()\n"
                'print("OTLP_ENABLED_IN_CHILD:", bool(probes))\n'
            ),
        )
    finally:
        if original_env is None:
            env_path.unlink(missing_ok=True)
        else:
            env_path.write_text(original_env)

    assert isinstance(result, ExecDone)
    assert result.output == "OTLP_ENABLED_IN_CHILD: False\n"
    child_log = log_path.read_bytes()[log_offset:] if log_path.exists() else b""
    assert b"OTLP backend init failed" not in child_log


async def test_subprocess_bootstrap_ignores_agent_package_in_process_cwd(tmp_path: Path) -> None:
    old_checkout = tmp_path / "old-checkout"
    (old_checkout / "agent").mkdir(parents=True)
    (old_checkout / "agent" / "__init__.py").write_text("# old checkout\n")

    result = await run_exec_child(
        tmp_path,
        ("import agent.execution.child as entry\nprint('exec-child', entry.__file__)\n"),
        parent=real_parent(
            cwd=old_checkout.resolve(), environ={**os.environ, "PYTHONPATH": str(old_checkout)}
        ),
    )

    assert isinstance(result, ExecDone)
    expected_entry = Path(__file__).resolve().parents[4] / "execution" / "child.py"
    assert str(expected_entry) in result.output


async def test_subprocess_bootstrap_forces_utf8_when_python_env_is_ignored(tmp_path: Path) -> None:
    result = await run_exec_child(
        tmp_path,
        (
            "import sys\n"
            "print('utf8-mode', sys.flags.utf8_mode)\n"
            "print('stdout-encoding', sys.stdout.encoding)\n"
            "print('cjk', '\u5b50\u8fdb\u7a0b')\n"
        ),
        parent=real_parent(environ={**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252"}),
    )

    assert isinstance(result, ExecDone)
    assert "utf8-mode 1" in result.output
    assert "stdout-encoding utf-8" in result.output.lower()
    assert "cjk \u5b50\u8fdb\u7a0b" in result.output


async def test_subprocess_merged_stream_preserves_order(tmp_path: Path) -> None:
    result = await run_exec_child(
        tmp_path,
        "import sys\nprint('one')\nprint('two', file=sys.stderr)\nprint('three')",
    )
    assert isinstance(result, ExecDone)
    assert result.output.index("one") < result.output.index("two") < result.output.index("three")


async def test_subprocess_output_contains_only_agent_text(tmp_path: Path) -> None:
    result = await run_exec_child(tmp_path, "print('ok')")
    assert isinstance(result, ExecDone)
    assert result.output == "ok\n"


async def test_subprocess_crashed_carries_child_traceback(tmp_path: Path) -> None:
    result = await run_exec_child(tmp_path, "raise ValueError('boom')")
    assert isinstance(result, ExecCrashed)
    assert isinstance(result.exc, ExecChildError)
    assert "boom" in (result.exc.exc_msg or "")
    assert "ValueError" in (result.exc.full_traceback or "")
    assert result.full_traceback is not None
    assert "ValueError: boom" in result.output


async def test_subprocess_os_exit_without_envelope_is_crash(tmp_path: Path) -> None:
    result = await run_exec_child(tmp_path, "import os\nos._exit(5)")
    assert isinstance(result, ExecCrashed)
    assert "without writing a result envelope" in str(result.exc)


@pytest.mark.parametrize("exit_code", [5, 124])
async def test_abrupt_root_exit_stops_descendant_before_reader_cleanup(
    tmp_path: Path, exit_code: int
) -> None:
    pid_file = tmp_path / f"abrupt-{exit_code}.pid"
    code = (
        "import os, pathlib, subprocess, sys\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(\n"
        "    str(descendant.pid), encoding='utf-8'\n"
        ")\n"
        f"os._exit({exit_code})\n"
    )
    descendant_pid: int | None = None
    try:
        result = await run_exec_child(tmp_path, code)
        assert isinstance(result, ExecCrashed)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        await _assert_tree_gone([descendant_pid])
        assert not any(thread.name == f"exec-reader-{AGENT_ID}" for thread in threading.enumerate())
    finally:
        if descendant_pid is None and pid_file.exists():
            descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        if descendant_pid is not None:
            kill_process_tree(descendant_pid, grace_s=0.0)


@pytest.mark.parametrize(
    ("fault", "trigger"),
    [
        ("sigkill", "os.kill(os.getpid(), signal.SIGKILL)"),
        ("segfault", "ctypes.string_at(0)"),
    ],
)
async def test_signal_killed_child_is_a_crash_and_the_host_keeps_serving(
    tmp_path: Path, fault: str, trigger: str
) -> None:
    pid_file = tmp_path / f"{fault}.pid"
    code = (
        "import ctypes, os, pathlib, signal, subprocess, sys\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(descendant.pid), encoding='utf-8')\n"
        "print('before the fault', flush=True)\n"
        f"{trigger}\n"
    )
    descendant_pid: int | None = None
    try:
        result = await run_exec_child(tmp_path, code)
        assert isinstance(result, ExecCrashed)
        assert "without writing a result envelope" in str(result.exc)
        assert "before the fault" in result.output
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        await _assert_tree_gone([descendant_pid])
        assert not any(thread.name == f"exec-reader-{AGENT_ID}" for thread in threading.enumerate())

        after = await run_exec_child(tmp_path, "print('still serving')")
        assert isinstance(after, ExecDone)
        assert after.output == "still serving\n"
    finally:
        if descendant_pid is None and pid_file.exists():
            descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        if descendant_pid is not None:
            kill_process_tree(descendant_pid, grace_s=0.0)


async def test_subprocess_timeout(tmp_path: Path) -> None:
    ready = tmp_path / "started.ready"
    code = (
        "import pathlib, time\n"
        "print('started', flush=True)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ok', encoding='utf-8')\n"
        "time.sleep(60)\n"
    )
    result = await run_exec_child(
        tmp_path,
        code,
        timeout=30.0,
        timers=_ExpireAfterReadyTimers(ready.exists, advance_s=31.0),
    )
    assert isinstance(result, ExecTimedOut)
    assert "started" in result.output


async def test_subprocess_cancel(tmp_path: Path) -> None:
    result = await run_exec_child(tmp_path, "import time\ntime.sleep(60)", cancel_after=0.4)
    assert isinstance(result, ExecCancelled)


async def test_natural_exit_reaps_ordinary_descendant_holding_stdout(tmp_path: Path) -> None:
    pid_file = tmp_path / "natural-descendant.pid"
    code = (
        "import pathlib, subprocess, sys\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(\n"
        "    str(descendant.pid), encoding='utf-8'\n"
        ")\n"
    )
    descendant_pid: int | None = None
    try:
        result = await run_exec_child(tmp_path, code)
        assert isinstance(result, ExecDone)
        descendant_pid = int(pid_file.read_text(encoding="utf-8"))
        await _assert_tree_gone([descendant_pid])
        assert not any(thread.name == f"exec-reader-{AGENT_ID}" for thread in threading.enumerate())
    finally:
        if descendant_pid is not None:
            kill_process_tree(descendant_pid, grace_s=0.0)


async def test_outer_task_cancel_reaps_child_and_descendant(tmp_path: Path) -> None:
    pid_file = tmp_path / "exec-tree.pids"
    code = (
        "import os, pathlib, subprocess, sys, time\n"
        "descendant = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(\n"
        "    f'{os.getpid()} {descendant.pid}', encoding='utf-8'\n"
        ")\n"
        "print('tree-ready', flush=True)\n"
        "time.sleep(60)\n"
    )
    task = asyncio.create_task(
        run_in_subprocess(
            code,
            exec_context(AGENT_ID),
            asyncio.Event(),
            60.0,
            exec_dir=tmp_path / "exec",
            parent=real_parent(),
            accumulation_max_chars=1_000_000,
            request_id=UUID(int=1),
            result_id=UUID(int=2),
        )
    )
    pids: list[int] = []
    try:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and not pid_file.exists():
            await asyncio.sleep(0.05)
        assert pid_file.exists(), "exec child never reached user code"
        pids = [int(value) for value in pid_file.read_text(encoding="utf-8").split()]

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15.0)

        await _assert_tree_gone(pids)
        assert not any(thread.name == f"exec-reader-{AGENT_ID}" for thread in threading.enumerate())
        assert not list((tmp_path / "exec" / str(AGENT_ID)).iterdir())
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for pid in reversed(pids):
            kill_process_tree(pid, grace_s=0.0)
        if pids:
            with contextlib.suppress(ChildProcessError, ProcessLookupError):
                os.waitpid(pids[0], 0)


async def test_subprocess_state_snapshot_reaches_child(tmp_path: Path) -> None:
    state = {"messages": [HumanMessage(content="snapshot says hi")], "halted": False}
    result = await run_exec_child(
        tmp_path,
        "import meiva\nprint(meiva.state.messages[0].content)",
        state=state,
    )
    assert isinstance(result, ExecDone)
    assert "snapshot says hi" in result.output


async def test_subprocess_unknown_lifecycle_class_crashes(tmp_path: Path) -> None:
    result = await run_exec_child(
        tmp_path,
        (
            "from base.agents.lifecycle import LifecycleExit\n"
            "class _MysteryLifecycle(LifecycleExit):\n"
            "    def __init__(self):\n"
            "        super().__init__(0)\n"
            "raise _MysteryLifecycle()\n"
        ),
    )

    assert isinstance(result, ExecCrashed)
    assert isinstance(result.exc, ExecChildError)
    assert result.exc.exc_type == "unknown_lifecycle_class"
    assert result.exc.exc_msg == ("child reported unknown lifecycle class '_MysteryLifecycle'")


async def test_subprocess_kills_process_group_on_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    pid_tmp = tmp_path / "grandchild.pid.tmp"
    code = (
        "import pathlib, subprocess, time\n"
        "p = subprocess.Popen(['sleep', '60'])\n"
        f"tmp = pathlib.Path({str(pid_tmp)!r})\n"
        "tmp.write_text(str(p.pid), encoding='utf-8')\n"
        f"tmp.replace({str(pid_file)!r})\n"
        "time.sleep(60)\n"
    )
    result = await run_exec_child(
        tmp_path,
        code,
        timeout=30.0,
        timers=_ExpireAfterReadyTimers(pid_file.exists, advance_s=31.0),
    )
    assert isinstance(result, ExecTimedOut)
    gc_pid = int(pid_file.read_text(encoding="utf-8"))
    await _assert_tree_gone([gc_pid])
