# meiva:note
# Keepalive watchdog daemon for cloudflared tunnels.
#
# Monitors either a child cloudflared process spawned by this watchdog or an existing
# host-level cloudflared tunnel service. Never kills external cloudflared processes on exit.

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import signal
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol

import psutil

from base.log import prepare_process_logging
from base.packages.plugins.config_registration import (
    disk_image_path,
    read_config_image,
    read_service_config,
)
from base.paths import meiva_home
from meiva_builtins.plugins.meiva_cloudflared.default_config import CloudflaredConfig
from meiva_builtins.plugins.meiva_cloudflared.service_declaration.declaration import (
    watchdog_pidfile_path,
    watchdog_socket_path,
)
from services.pidfile import acquire_pidfile, remove_pidfile

logger = logging.getLogger(__name__)

_DAEMON_MODULE = "meiva_builtins.plugins.meiva_cloudflared.service_declaration.daemon"
_PING_REPLY = b'{"id":0,"ok":true}\n'
_DEFAULT_BIN_PATHS: tuple[Path, ...] = (
    Path("/opt/homebrew/bin/cloudflared"),
    Path("/usr/local/bin/cloudflared"),
    Path("/usr/bin/cloudflared"),
)


class ManagedProcess(Protocol):
    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


@dataclass(frozen=True)
class ProcessSnapshot:
    pid: int
    name: str
    cmdline: tuple[str, ...]


@dataclass
class WatchdogState:
    managed_proc: ManagedProcess | None = None
    logged_missing_bin: bool = False
    logged_missing_config: bool = False
    last_external_pid: int | None = None


def list_running_processes() -> list[ProcessSnapshot]:
    snapshots: list[ProcessSnapshot] = []
    for proc in psutil.process_iter():
        try:
            snapshots.append(
                ProcessSnapshot(
                    pid=proc.pid,
                    name=proc.name(),
                    cmdline=tuple(proc.cmdline()),
                )
            )
        except (psutil.Error, OSError, ValueError):
            continue
    return snapshots


def is_cloudflared_tunnel_process(proc: ProcessSnapshot, *, self_pid: int) -> bool:
    if proc.pid == self_pid:
        return False
    name_lower = proc.name.lower()
    exe_name = Path(proc.cmdline[0]).name.lower() if proc.cmdline else ""
    if name_lower != "cloudflared" and exe_name != "cloudflared":
        return False
    return "tunnel" in proc.cmdline


def find_external_cloudflared_pid(
    processes: Sequence[ProcessSnapshot],
    *,
    self_pid: int,
    managed_pid: int | None = None,
) -> int | None:
    for proc in processes:
        if managed_pid is not None and proc.pid == managed_pid:
            continue
        if is_cloudflared_tunnel_process(proc, self_pid=self_pid):
            return proc.pid
    return None


def resolve_cloudflared_binary(
    configured_bin: str,
    *,
    which: Callable[[str], str | None],
    fallback_paths: Sequence[Path] = _DEFAULT_BIN_PATHS,
) -> str | None:
    candidate_str = configured_bin.strip()
    if not candidate_str:
        return None
    candidate_path = Path(candidate_str)
    if candidate_path.is_absolute():
        return str(candidate_path) if candidate_path.is_file() else None
    found = which(candidate_str)
    if found:
        return found
    if candidate_str == "cloudflared":
        for fallback in fallback_paths:
            if fallback.is_file():
                return str(fallback)
    return None


def _expand_user_path(raw_path: str, *, user_home: Path) -> Path:
    if raw_path == "~":
        return user_home
    if raw_path.startswith("~/"):
        return user_home / raw_path[2:]
    return Path(raw_path)


def resolve_cloudflared_config_path(
    configured_path: str,
    *,
    user_home: Path,
    system_config_dir: Path = Path("/etc/cloudflared"),
) -> Path | None:
    explicit = configured_path.strip()
    if explicit:
        path = _expand_user_path(explicit, user_home=user_home)
        return path if path.is_file() else None
    candidates = (
        user_home / ".cloudflared" / "config.yml",
        user_home / ".cloudflared" / "config.yaml",
        system_config_dir / "config.yml",
        system_config_dir / "config.yaml",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def build_cloudflared_command(
    bin_path: str,
    config: CloudflaredConfig,
    *,
    resolved_config_path: Path | None,
) -> list[str]:
    cmd = [bin_path, "--no-autoupdate"]
    if resolved_config_path is not None:
        cmd.extend(["--config", str(resolved_config_path)])
    cmd.append("tunnel")
    cmd.extend(config.cloudflared_extra_args)
    cmd.append("run")
    tunnel = config.cloudflared_tunnel.strip()
    if tunnel:
        cmd.append(tunnel)
    return cmd


async def _spawn_cloudflared_process(cmd: Sequence[str]) -> ManagedProcess:
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
    )


async def reconcile_cloudflared_once(
    state: WatchdogState,
    config: CloudflaredConfig,
    *,
    self_pid: int,
    user_home: Path,
    which: Callable[[str], str | None],
    process_lister: Callable[[], Sequence[ProcessSnapshot]] = list_running_processes,
    spawner: Callable[[Sequence[str]], Awaitable[ManagedProcess]] = _spawn_cloudflared_process,
    fallback_bin_paths: Sequence[Path] = _DEFAULT_BIN_PATHS,
    system_config_dir: Path = Path("/etc/cloudflared"),
) -> None:
    if state.managed_proc is not None:
        if state.managed_proc.returncode is None:
            return
        logger.warning(
            "Managed cloudflared process (pid=%s) exited with code %s",
            state.managed_proc.pid,
            state.managed_proc.returncode,
        )
        state.managed_proc = None

    external_pid = find_external_cloudflared_pid(
        process_lister(),
        self_pid=self_pid,
    )
    if external_pid is not None:
        if state.last_external_pid != external_pid:
            logger.info(
                "Detected external cloudflared tunnel process (pid=%s); monitoring without spawning duplicate",
                external_pid,
            )
            state.last_external_pid = external_pid
        return
    state.last_external_pid = None

    bin_path = resolve_cloudflared_binary(
        config.cloudflared_bin,
        which=which,
        fallback_paths=fallback_bin_paths,
    )
    if bin_path is None:
        if not state.logged_missing_bin:
            logger.info(
                "cloudflared binary (%s) not found on host; watchdog idling",
                config.cloudflared_bin,
            )
            state.logged_missing_bin = True
        return
    state.logged_missing_bin = False

    resolved_config = resolve_cloudflared_config_path(
        config.cloudflared_config_path,
        user_home=user_home,
        system_config_dir=system_config_dir,
    )
    if resolved_config is None and not config.cloudflared_tunnel.strip():
        if not state.logged_missing_config:
            logger.info(
                "No cloudflared config file or tunnel configured on host; watchdog idling",
            )
            state.logged_missing_config = True
        return
    state.logged_missing_config = False

    cmd = build_cloudflared_command(
        bin_path,
        config,
        resolved_config_path=resolved_config,
    )
    try:
        proc = await spawner(cmd)
    except OSError as exc:
        logger.warning("Failed to spawn cloudflared (%s): %s", cmd, exc)
        return
    state.managed_proc = proc
    logger.info("Spawned managed cloudflared tunnel process (pid=%s)", proc.pid)


async def terminate_managed_child(
    proc: ManagedProcess | None,
    *,
    grace_seconds: float = 5.0,
) -> None:
    if proc is None or proc.returncode is not None:
        return
    logger.info("Terminating managed cloudflared child process (pid=%s)", proc.pid)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.terminate()
    try:
        _ = await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
    except TimeoutError:
        logger.warning("Managed cloudflared child (pid=%s) did not exit; killing", proc.pid)
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(OSError):
            _ = await proc.wait()


async def _handle_probe_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        with contextlib.suppress(TimeoutError, OSError):
            _ = await asyncio.wait_for(reader.readline(), timeout=2.0)
            writer.write(_PING_REPLY)
            await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def start_watchdog_socket_server(socket_path: Path) -> asyncio.AbstractServer:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()
    return await asyncio.start_unix_server(_handle_probe_client, path=str(socket_path))


async def close_watchdog_socket_server(
    server: asyncio.AbstractServer | None,
    socket_path: Path,
) -> None:
    if server is not None:
        server.close()
        with contextlib.suppress(OSError):
            await server.wait_closed()
    with contextlib.suppress(FileNotFoundError):
        socket_path.unlink()


async def _run_daemon(
    *,
    home: Path,
    user_home: Path,
    self_pid: int,
    path_env: str | None,
    config: CloudflaredConfig,
) -> None:
    pidfile = watchdog_pidfile_path(home)
    socket_path = watchdog_socket_path(home)
    if not acquire_pidfile(pidfile, _DAEMON_MODULE, pid=self_pid):
        logger.info("Another cloudflared watchdog instance already holds %s; exiting", pidfile)
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    state = WatchdogState()
    which_fn = partial(shutil.which, path=path_env)
    server: asyncio.AbstractServer | None = None
    try:
        server = await start_watchdog_socket_server(socket_path)
        logger.info(
            "cloudflared keepalive watchdog started (interval=%.1fs, socket=%s)",
            config.cloudflared_check_interval_seconds,
            socket_path,
        )
        while not stop_event.is_set():
            await reconcile_cloudflared_once(
                state,
                config,
                self_pid=self_pid,
                user_home=user_home,
                which=which_fn,
            )
            try:
                _ = await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=config.cloudflared_check_interval_seconds,
                )
            except TimeoutError:
                continue
    finally:
        await terminate_managed_child(state.managed_proc)
        await close_watchdog_socket_server(server, socket_path)
        remove_pidfile(pidfile)
        logger.info("cloudflared keepalive watchdog stopped")


def main() -> None:
    home = meiva_home()
    user_home = Path.home()
    self_pid = os.getpid()
    path_env = os.environ.get("PATH")
    config = read_service_config("meiva_cloudflared", CloudflaredConfig, environ=os.environ)
    if config is None:
        config = read_config_image(
            CloudflaredConfig,
            disk_image_path("meiva_cloudflared", home=home),
        )
    asyncio.run(
        _run_daemon(
            home=home,
            user_home=user_home,
            self_pid=self_pid,
            path_env=path_env,
            config=config,
        )
    )


if __name__ == "__main__":
    prepare_process_logging()
    main()
