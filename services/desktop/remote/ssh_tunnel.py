from __future__ import annotations

import socket
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeGuard

from base.clock import Timers
from base.host.boundary.untyped import object_of
from base.host.permissions_helper.client import PermissionsHelperError
from base.host.remote_hosts import RemoteHostConfig

from .plan import ssh_tunnel_argv

_TUNNEL_STARTUP_TIMEOUT_S = 6.0
_TUNNEL_POLL_S = 0.05


@dataclass
class _ManagedTunnel:
    local_port: int
    helper_port: int
    ssh_host: str
    proc: subprocess.Popen[bytes]


def _is_object_tuple(value: object) -> TypeGuard[tuple[object, ...]]:
    return isinstance(value, tuple)


def _pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        addr = object_of(s.getsockname)
        if not _is_object_tuple(addr) or len(addr) < 2:
            raise OSError("unexpected loopback port binding")
        port = addr[1]
        if not isinstance(port, int) or isinstance(port, bool):
            raise OSError("unexpected loopback port binding")
        return port


def _port_accepting(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _launch_ssh(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )


class SshTunnelManager:
    def __init__(
        self,
        *,
        timers: Timers,
        startup_timeout_s: float = _TUNNEL_STARTUP_TIMEOUT_S,
        launch: Callable[[list[str]], subprocess.Popen[bytes]] = _launch_ssh,
        port_accepting: Callable[[int], bool] = _port_accepting,
    ) -> None:
        self._timers = timers
        self._startup_timeout_s = startup_timeout_s
        self._launch = launch
        self._port_accepting = port_accepting
        self._lock = threading.Lock()
        self._tunnels: dict[tuple[str, bool], _ManagedTunnel] = {}
        self._stop_event = threading.Event()

    def endpoint_for(self, config: RemoteHostConfig, *, browser: bool = False) -> str:
        if config.local_port is not None and not browser:
            return f"http://127.0.0.1:{config.local_port}"
        key = (config.alias, browser)
        remote_port = config.browser_port if browser else config.helper_port
        with self._lock:
            if self._stop_event.is_set():
                raise PermissionsHelperError("SSH tunnel manager is closed")
            existing = self._tunnels.get(key)
            if (
                existing is not None
                and existing.proc.poll() is None
                and existing.ssh_host == config.ssh_host
                and existing.helper_port == remote_port
            ):
                return f"http://127.0.0.1:{existing.local_port}"
            if existing is not None:
                self._stop_tunnel(existing)
                self._tunnels.pop(key, None)
            tunnel = self._start_tunnel(config, remote_port=remote_port, owned=browser)
            self._tunnels[key] = tunnel
            return f"http://127.0.0.1:{tunnel.local_port}"

    def _start_tunnel(
        self, config: RemoteHostConfig, *, remote_port: int, owned: bool
    ) -> _ManagedTunnel:
        local_port = _pick_free_loopback_port()
        argv = ssh_tunnel_argv(
            config.ssh_host, local_port=local_port, remote_port=remote_port, owned=owned
        )
        try:
            proc = self._launch(argv)
        except OSError as exc:
            raise PermissionsHelperError(
                f"failed to start ssh tunnel for {config.target_uri}: {exc}"
            ) from exc

        deadline = self._timers.monotonic() + self._startup_timeout_s
        while self._timers.monotonic() < deadline and not self._stop_event.is_set():
            if proc.poll() is not None:
                stderr = (
                    (proc.stderr.read() if proc.stderr else b"")
                    .decode("utf-8", errors="replace")
                    .strip()
                )
                self._close_stderr(proc)
                raise PermissionsHelperError(
                    f"ssh tunnel for {config.target_uri} exited immediately "
                    f"(code={proc.returncode}): {stderr or 'no stderr'}"
                )
            if self._port_accepting(local_port) and proc.poll() is None:
                return _ManagedTunnel(
                    local_port=local_port,
                    helper_port=remote_port,
                    ssh_host=config.ssh_host,
                    proc=proc,
                )
            if self._stop_event.wait(_TUNNEL_POLL_S):
                break

        self._stop_tunnel(
            _ManagedTunnel(
                local_port=local_port,
                helper_port=remote_port,
                ssh_host=config.ssh_host,
                proc=proc,
            )
        )
        raise PermissionsHelperError(
            f"ssh tunnel for {config.target_uri} did not become ready within "
            f"{self._startup_timeout_s:g}s"
        )

    def _stop_tunnel(self, tunnel: _ManagedTunnel) -> None:
        if tunnel.proc.poll() is None:
            tunnel.proc.terminate()
            try:
                tunnel.proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                tunnel.proc.kill()
                tunnel.proc.wait(timeout=2.0)
        self._close_stderr(tunnel.proc)

    @staticmethod
    def _close_stderr(proc: subprocess.Popen[bytes]) -> None:
        if proc.stderr is not None:
            proc.stderr.close()

    def close_all(self) -> None:
        self._stop_event.set()
        with self._lock:
            tunnels = list(self._tunnels.values())
            self._tunnels.clear()
        for tunnel in tunnels:
            self._stop_tunnel(tunnel)
