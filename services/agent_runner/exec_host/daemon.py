# meiva:note
# exec-host daemon entry: `python -m services.agent_runner.exec_host.daemon`.
#
# Materializes the unit environment (`ConfigBoot().boot()`), takes the home's
# instance lock (`run/exec-host.lock`), and serves `exec-host.sock` alongside the
# host memory guard. Clients own their envelopes, including direct fallback runs
# that can remain active while this daemon starts.

from __future__ import annotations

import asyncio
import contextlib
import os
import resource
import sys
from pathlib import Path

from base.config import ConfigBoot
from base.deploy.release import editable_install
from base.log import logger
from base.log.sinks import add_sink
from base.native_process import child_env
from base.native_process.os_platform import LockTimeoutError, file_lock
from base.paths import meiva_home, run_dir
from base.sessions.exec_host.paths import fallback_dir, lock_path, service_socket_path
from services.agent_runner.exec_host import shutdown_budget
from services.agent_runner.exec_host.memory_guard import run_memory_guard_forever
from services.agent_runner.exec_host.service import ExecHostService

_NOFILE_TARGET = 10240


def _raise_nofile_limit() -> None:
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = _NOFILE_TARGET if hard == resource.RLIM_INFINITY else min(hard, _NOFILE_TARGET)
    if soft < wanted:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))


def _own_fallback_dir(directory: Path) -> bool:
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        logger.error("{directory} is not an owner-only directory of this user", directory=directory)
        return False
    return True


def run_with_home(home: Path, *, default_cwd: Path) -> int:
    run = run_dir(home)
    try:
        with file_lock(lock_path(run), timeout_s=0):
            return _run(home, run, default_cwd=default_cwd)
    except LockTimeoutError:
        logger.error("another exec-host service holds {lock}", lock=lock_path(run))
        return 1


def main() -> int:
    add_sink(
        sys.stderr,
        format="{time:HH:mm:ss.SSS} <level>{level: <5}</level> {message}",
        level="INFO",
        colorize=False,
    )
    ConfigBoot().boot()
    _raise_nofile_limit()
    return run_with_home(meiva_home(), default_cwd=Path.cwd())


async def _serve_with_guard(service: ExecHostService, path: Path, drain_s: float) -> None:
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(run_memory_guard_forever(service.stop_event))
        await service.serve(path, drain_timeout_s=drain_s)


def _run(home: Path, run: Path, *, default_cwd: Path) -> int:
    service = ExecHostService(
        home=home,
        base_environ=child_env.inherited_process_env(),
        default_cwd=default_cwd,
        source_root=editable_install.current_interpreter_source_root(),
    )
    path = service_socket_path(run)
    if path.parent == fallback_dir() and not _own_fallback_dir(path.parent):
        return 1
    drain_s = shutdown_budget.SHUTDOWN_TERM_WAIT_S + shutdown_budget.SHUTDOWN_KILL_S
    asyncio.run(_serve_with_guard(service, path, drain_s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
