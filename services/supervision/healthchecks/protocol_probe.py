"""Bounded application protocol observations; lifecycle owners handle recovery."""

from __future__ import annotations

import json
import socket
from pathlib import Path

from base.daemon.health import DaemonProbe


def ping(path: Path | str) -> DaemonProbe:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect(str(path))
        sock.sendall(b'{"id":0,"method":"ping","agent_id":null}\n')
        response = bytearray()
        while b"\n" not in response:
            chunk = sock.recv(4096)
            if not chunk:
                return DaemonProbe.down("ping stream closed before a response")
            response.extend(chunk)
            if len(response) > 65536:
                return DaemonProbe.down("ping response exceeds the protocol limit")
        payload = json.loads(response.split(b"\n", 1)[0])
        if payload["id"] != 0 or payload["ok"] is not True:
            return DaemonProbe.down("Unix endpoint rejected ping")
        return DaemonProbe.up("Unix endpoint answered ping")


def _observe(service: str, home: Path, *, browser_cdp_port: int) -> DaemonProbe:
    from base.paths import (
        chrome_mcp_socket,
        computer_mcp_socket,
        mcp_daemon_shared_socket,
        run_dir,
    )
    from base.sessions.exec_host.paths import service_socket_path as exec_host_socket_path
    from base.sessions.pty.paths import service_socket_path

    sockets = {
        "browser-mcp": lambda: chrome_mcp_socket(home, browser_cdp_port),
        "computer-mcp": lambda: computer_mcp_socket(home),
        "exec-host": lambda: exec_host_socket_path(run_dir(home)),
        "mcp-daemon": lambda: mcp_daemon_shared_socket(home),
        "pty-sessions": lambda: service_socket_path(run_dir(home)),
    }
    if service in sockets:
        return ping(sockets[service]())
    if service == "insights":
        from services.supervision.healthchecks.insights import insights_probe

        return insights_probe(home)
    raise ValueError(f"unsupported protocol probe: {service}")


def probe(service: str, *, home: Path, browser_cdp_port: int) -> DaemonProbe:
    """Observe the endpoint of `service` in `home` through its application protocol."""
    try:
        return _observe(service, home, browser_cdp_port=browser_cdp_port)
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        return DaemonProbe.down(f"protocol probe failed: {exc}")
