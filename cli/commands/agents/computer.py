# meiva:note
# `meiva computer`: operator and agent discovery/control commands for computer-use targets.
#
# - `meiva computer list [--json]`: progressive discovery of `local`, registered cluster desktop
#   machines (`meiva://<machine>`), and external SSH hosts from `$MEIVA_HOME/remote_hosts.toml`
#   (`ssh://<alias>`), including lightweight health and lease-holder probing with zero secret
#   disclosure.
# - `meiva computer release [--host <target>]`: operator force-release of the screen lease on
#   local or a named remote host.

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from base.host.boundary.json_values import is_object_dict
from base.host.remote_hosts import RemoteHostConfigError, load_remote_hosts
from base.paths import computer_mcp_socket
from cli.boundary.json_body import loads
from services.desktop.remote.win_bridge import probe_win_bridge_health

_LOCAL_SOCKET_TIMEOUT_S = 2.0


@dataclass(frozen=True)
class ComputerTargetRow:
    target: str
    os: str
    status: str
    held_by: str
    notes: str
    browser_status: str = "unknown"

    def as_dict(self) -> dict[str, str | None]:
        return {
            "target": self.target,
            "os": self.os,
            "status": self.status,
            "held_by": None if self.held_by == "-" else self.held_by,
            "notes": self.notes,
            "browser_status": self.browser_status,
        }


def _format_holder(raw: object) -> str:
    if raw is None or raw == "":
        return "-"
    if isinstance(raw, int) and not isinstance(raw, bool):
        return f"agent:{raw}"
    return str(raw)


def format_computer_table(rows: Sequence[ComputerTargetRow]) -> str:
    headers = ("TARGET", "OS", "STATUS", "HELD_BY", "CHROME", "NOTES / CAPABILITIES")
    matrix = [(r.target, r.os, r.status, r.held_by, r.browser_status, r.notes) for r in rows]
    widths = [
        max([len(headers[col]), *(len(row[col]) for row in matrix)]) for col in range(len(headers))
    ]
    lines = [
        "  ".join(headers[i].ljust(widths[i]) for i in range(len(headers))).rstrip(),
        "  ".join("-" * widths[i] for i in range(len(headers))).rstrip(),
    ]
    for row in matrix:
        lines.append("  ".join(row[i].ljust(widths[i]) for i in range(len(headers))).rstrip())
    return "\n".join(lines)


def _probe_local_daemon_status(home: Path) -> dict[str, object] | None:
    sock_path = str(computer_mcp_socket(home))
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(_LOCAL_SOCKET_TIMEOUT_S)
            s.connect(sock_path)
            stream = s.makefile("rwb")
            stream.write((json.dumps({"id": 1, "method": "status"}) + "\n").encode("utf-8"))
            stream.flush()
            line = stream.readline()
    except OSError:
        return None
    if not line:
        return None
    try:
        resp = loads(line)
    except ValueError:
        return None
    if not isinstance(resp, dict) or resp.get("ok") is not True:
        return None
    result = resp.get("result")
    return dict(result) if isinstance(result, dict) else None


def _collect_local_rows(
    home: Path,
    platform_system: str,
    local_machine: str | None,
    local_description: str | None,
    *,
    browser_available: bool | None,
) -> list[ComputerTargetRow]:
    local_os = "macos" if platform_system == "darwin" else platform_system
    local_status = _probe_local_daemon_status(home)
    chrome = _browser_status(available=browser_available)
    if local_status is not None and bool(local_status.get("local_available", False)):
        return [
            ComputerTargetRow(
                target="local",
                os=local_os,
                status="online",
                held_by=_format_holder(local_status.get("holder")),
                browser_status=chrome,
                notes=(local_description or f"local desktop ({local_machine or local_os})").strip(),
            )
        ]
    if (local_status is None and platform_system == "darwin") or browser_available is True:
        return [
            ComputerTargetRow(
                target="local",
                os=local_os,
                status="offline",
                held_by="-",
                browser_status=chrome,
                notes=(local_description or "local desktop (computer-mcp not running)").strip(),
            )
        ]
    return []


def _browser_status(*, available: bool | str | None) -> str:
    if isinstance(available, str):
        return available
    if available is None:
        return "unknown"
    return "online" if available else "offline"


def _probe_cluster_row(
    machine_name: str,
    desc: str | None,
    probed: dict[str, object],
    *,
    browser_available: bool | str | None,
) -> ComputerTargetRow | None:
    note = (desc or "").strip()
    if (
        probed.get("ok") is True
        and probed.get("available") is False
        and browser_available is not True
        and browser_available != "upgrade runner"
    ):
        return None
    peer_os = str(probed.get("os") or "unknown")
    online = probed.get("ok") is True and probed.get("available") is True
    return ComputerTargetRow(
        target=f"meiva://{machine_name}",
        os=peer_os,
        status="online" if online else "offline",
        held_by=_format_holder(probed.get("holder")),
        notes=note,
        browser_status=_browser_status(available=browser_available),
    )


def _probe_value(future: Future[object] | None) -> object:
    if future is None or not future.done() or future.cancelled():
        return None
    return future.result()


def _cluster_rows(
    machines: Sequence[tuple[str, str | None, str | None]],
    computer_probe: Callable[[str], dict[str, object]] | None,
    browser_probe: Callable[[str], bool | str | None] | None,
) -> list[ComputerTargetRow]:
    pool = ThreadPoolExecutor(max_workers=8)

    def run(probe: Callable[[str], object], url: str) -> object:
        return probe(url)

    computers: dict[str, Future[object]] = {}
    browsers: dict[str, Future[object]] = {}
    try:
        for name, url, _desc in machines:
            if url and computer_probe is not None:
                computers[name] = pool.submit(run, computer_probe, url)
            if url and browser_probe is not None:
                browsers[name] = pool.submit(run, browser_probe, url)
        _ = wait([*computers.values(), *browsers.values()], timeout=3.0)
        rows: list[ComputerTargetRow] = []
        for name, _url, desc in machines:
            desktop = _probe_value(computers.get(name))
            chrome = _probe_value(browsers.get(name))
            row = _probe_cluster_row(
                name,
                desc,
                desktop if is_object_dict(desktop) else {},
                browser_available=chrome if isinstance(chrome, bool | str) else None,
            )
            if row is not None:
                rows.append(row)
        return rows
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _collect_ssh_rows(
    home: Path,
    ssh_health_probe: Callable[[str], dict[str, object] | None],
) -> list[ComputerTargetRow]:
    remote_hosts = load_remote_hosts(home=home)
    rows: list[ComputerTargetRow] = []
    for alias in sorted(remote_hosts):
        cfg = remote_hosts[alias]
        status_label, held_by = "configured (on-demand ssh)", "-"
        if cfg.local_port is not None:
            health = ssh_health_probe(f"http://127.0.0.1:{cfg.local_port}")
            if health is not None and bool(health.get("ok", False)):
                status_label = (
                    "aborted (corner failsafe)"
                    if bool(health.get("session_aborted", False))
                    else "online"
                )
                held_by = _format_holder(health.get("lease_holder"))
            else:
                status_label = "offline"
        rows.append(
            ComputerTargetRow(
                cfg.target_uri, cfg.os, status_label, held_by, cfg.summary_notes, "unprobed (SSH)"
            )
        )
    return rows


def cmd_computer_list(
    *,
    home: Path,
    platform_system: str,
    local_machine: str | None = None,
    local_description: str | None = None,
    cluster_machines: Sequence[tuple[str, str | None, str | None]] = (),
    cluster_status_probe: Callable[[str], dict[str, object]] | None = None,
    browser_status_probe: Callable[[str], bool | str | None] | None = None,
    local_browser_available: bool | None = None,
    ssh_health_probe: Callable[[str], dict[str, object] | None] = probe_win_bridge_health,
    json_output: bool = False,
) -> int:
    rows = _collect_local_rows(
        home,
        platform_system,
        local_machine,
        local_description,
        browser_available=local_browser_available,
    )
    rows.extend(
        _cluster_rows(
            [machine for machine in cluster_machines if machine[0] != local_machine],
            cluster_status_probe,
            browser_status_probe,
        )
    )
    try:
        rows.extend(_collect_ssh_rows(home, ssh_health_probe))
    except RemoteHostConfigError as exc:
        print(f"invalid remote_hosts.toml: {exc}", file=sys.stderr)
        return 1
    if json_output:
        print(json.dumps([r.as_dict() for r in rows], ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print(
            "No computer-use targets available. Configure $MEIVA_HOME/remote_hosts.toml or start computer-mcp."
        )
        return 0
    print(format_computer_table(rows))
    return 0


# meiva:note
# Force-release the screen from its current holder. `home` is the one the invocation settled
# on; the daemon's socket lives under it.
def cmd_computer_release(*, home: Path, host: str | None = None) -> int:
    sock_path = str(computer_mcp_socket(home))
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(sock_path)
    except (FileNotFoundError, ConnectionRefusedError) as e:
        print(f"computer-mcp daemon not reachable at {sock_path}: {e}", file=sys.stderr)
        return 1
    args_payload: dict[str, object] = {"force": True}
    if host:
        args_payload["host"] = host
    with s:
        stream = s.makefile("rwb")
        req = {
            "id": 1,
            "method": "call_tool",
            "tool": "release_control",
            "args": args_payload,
        }
        stream.write((json.dumps(req) + "\n").encode())
        stream.flush()
        line = stream.readline()
    if not line:
        print("computer-mcp daemon closed the connection", file=sys.stderr)
        return 1
    try:
        resp = loads(line)
    except ValueError:
        resp = None
    if not isinstance(resp, dict):
        print("computer-mcp daemon returned a corrupted response", file=sys.stderr)
        return 1
    if resp.get("ok") is not True:
        print(f"release failed: {resp.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    result: JsonValue = None
    match resp.get("result"):
        case {"content": [{"text": str(text)}, *_]}:
            try:
                result = loads(text)
            except ValueError:
                result = None
        case _:
            result = None
    match result:
        case {"released": None, "remote_released": True}:
            print("released the remote screen lease")
            return 0
        case {"released": None}:
            print("screen was free (no holder)")
            return 0
        case {"released": int(released)} if not isinstance(released, bool):
            print(f"released the screen from agent {released}")
            return 0
        case _:
            print("computer-mcp daemon returned an unexpected release payload", file=sys.stderr)
            return 1
