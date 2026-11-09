# meiva:note
# Keepalive watchdog daemon for Tailscale mesh VPN connectivity.
#
# Periodically checks `tailscale status --json` and brings Tailscale back up when stopped.
# On shutdown (SIGTERM/SIGINT), stops only the keepalive watchdog and never runs `tailscale down`
# or terminates `tailscaled` / `Tailscale.app`.

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.log import prepare_process_logging
from base.packages.plugins.config_registration import (
    disk_image_path,
    read_config_image,
    read_service_config,
)
from base.paths import meiva_home
from meiva_builtins.plugins.meiva_tailscale.default_config import TailscaleConfig
from meiva_builtins.plugins.meiva_tailscale.service_declaration.declaration import (
    watchdog_pidfile_path,
    watchdog_socket_path,
)
from services.pidfile import acquire_pidfile, remove_pidfile

logger = logging.getLogger(__name__)

_DAEMON_MODULE = "meiva_builtins.plugins.meiva_tailscale.service_declaration.daemon"
_PING_REPLY = b'{"id":0,"ok":true}\n'
_DEFAULT_BIN_PATHS: tuple[Path, ...] = (
    Path("/usr/local/bin/tailscale"),
    Path("/opt/homebrew/bin/tailscale"),
    Path("/usr/bin/tailscale"),
    Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
)
_MACOS_APP_PATH = Path("/Applications/Tailscale.app")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class WatchdogState:
    logged_missing_bin: bool = False
    last_backend_state: str | None = None


def resolve_tailscale_binary(
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
    if candidate_str == "tailscale":
        for fallback in fallback_paths:
            if fallback.is_file():
                return str(fallback)
    return None


def parse_tailscale_backend_state(raw_stdout: str) -> str | None:
    stripped = raw_stdout.strip()
    if not stripped:
        return None
    try:
        payload = object_of(json.loads, stripped)
    except (ValueError, TypeError):
        return None
    if not is_object_dict(payload):
        return None
    backend_state = payload.get("BackendState")
    return backend_state if isinstance(backend_state, str) else None


async def run_command_with_timeout(
    cmd: Sequence[str],
    *,
    timeout_seconds: float,
) -> CommandResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return CommandResult(returncode=-1, stdout="", stderr=str(exc))

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(OSError):
            _ = await proc.wait()
        return CommandResult(returncode=-1, stdout="", stderr="command timed out")

    return CommandResult(
        returncode=0 if proc.returncode is None else proc.returncode,
        stdout=stdout_bytes.decode("utf-8", errors="replace"),
        stderr=stderr_bytes.decode("utf-8", errors="replace"),
    )


async def reconcile_tailscale_once(
    state: WatchdogState,
    config: TailscaleConfig,
    *,
    which: Callable[[str], str | None],
    is_darwin: bool,
    runner: Callable[..., Awaitable[CommandResult]] = run_command_with_timeout,
    fallback_bin_paths: Sequence[Path] = _DEFAULT_BIN_PATHS,
    macos_app_path: Path = _MACOS_APP_PATH,
) -> None:
    bin_path = resolve_tailscale_binary(
        config.tailscale_bin,
        which=which,
        fallback_paths=fallback_bin_paths,
    )
    if bin_path is None:
        if not state.logged_missing_bin:
            logger.info(
                "tailscale binary (%s) not found on host; watchdog idling",
                config.tailscale_bin,
            )
            state.logged_missing_bin = True
        return
    state.logged_missing_bin = False

    status_res = await runner(
        [bin_path, "status", "--json"],
        timeout_seconds=10.0,
    )
    backend_state = parse_tailscale_backend_state(status_res.stdout)
    state.last_backend_state = backend_state

    if status_res.returncode == 0 and backend_state == "Running":
        return

    if (
        status_res.returncode != 0
        and backend_state is None
        and is_darwin
        and macos_app_path.exists()
    ):
        logger.info("Tailscale daemon unreachable on macOS; launching Tailscale.app in background")
        _ = await runner(
            ["open", "-g", "-a", "Tailscale"],
            timeout_seconds=10.0,
        )

    if backend_state != "Running" and config.tailscale_auto_up:
        up_cmd = [bin_path, "up", *config.tailscale_up_args]
        logger.info(
            "Tailscale BackendState=%r (exit=%s); running %s",
            backend_state,
            status_res.returncode,
            up_cmd,
        )
        up_res = await runner(up_cmd, timeout_seconds=15.0)
        if up_res.returncode != 0:
            logger.warning(
                "tailscale up exited with code %s: %s",
                up_res.returncode,
                up_res.stderr.strip(),
            )


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
    self_pid: int,
    path_env: str | None,
    is_darwin: bool,
    config: TailscaleConfig,
) -> None:
    pidfile = watchdog_pidfile_path(home)
    socket_path = watchdog_socket_path(home)
    if not acquire_pidfile(pidfile, _DAEMON_MODULE, pid=self_pid):
        logger.info("Another tailscale watchdog instance already holds %s; exiting", pidfile)
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
            "tailscale keepalive watchdog started (interval=%.1fs, socket=%s)",
            config.tailscale_check_interval_seconds,
            socket_path,
        )
        while not stop_event.is_set():
            await reconcile_tailscale_once(
                state,
                config,
                which=which_fn,
                is_darwin=is_darwin,
            )
            try:
                _ = await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=config.tailscale_check_interval_seconds,
                )
            except TimeoutError:
                continue
    finally:
        await close_watchdog_socket_server(server, socket_path)
        remove_pidfile(pidfile)
        logger.info("tailscale keepalive watchdog stopped (Tailscale interface left untouched)")


def main() -> None:
    home = meiva_home()
    self_pid = os.getpid()
    path_env = os.environ.get("PATH")
    is_darwin = sys.platform == "darwin"
    config = read_service_config("meiva_tailscale", TailscaleConfig, environ=os.environ)
    if config is None:
        config = read_config_image(
            TailscaleConfig,
            disk_image_path("meiva_tailscale", home=home),
        )
    asyncio.run(
        _run_daemon(
            home=home,
            self_pid=self_pid,
            path_env=path_env,
            is_darwin=is_darwin,
            config=config,
        )
    )


if __name__ == "__main__":
    prepare_process_logging()
    main()
