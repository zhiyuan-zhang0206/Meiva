from __future__ import annotations

import errno
import os
import shutil
import socket
from collections.abc import Collection
from ipaddress import IPv6Address, ip_address

import base.host.proc
from base.cluster.listener_plan import (
    LOOPBACK_ALIASES,
    LSOF_CANDIDATE_PATHS,
    ListenerDiscoveryError,
    TcpBind,
    parse_lsof_listener_addrs,
    parse_lsof_pids,
    parse_psutil_listener_addrs,
    reconcile_strict_listeners,
    resolve_lsof_argv,
)
from base.cluster.machine import MachineIdentityReader
from base.native_process.ownership import OwnedProcess, capture_tree

__all__ = [
    "LOOPBACK_ALIASES",
    "LSOF_CANDIDATE_PATHS",
    "ListenerDiscoveryError",
    "bind_addrs",
    "listener_addrs",
    "lsof_argv",
    "require_listener",
    "serves_off_box",
    "strict_listeners_on",
    "verify_listener_custody",
]


def lsof_argv(*args: str) -> list[str] | None:
    path_binary = shutil.which("lsof")
    executable_candidates = (
        [c for c in LSOF_CANDIDATE_PATHS if os.access(c, os.X_OK)] if path_binary is None else ()
    )
    return resolve_lsof_argv(
        *args,
        path_binary=path_binary,
        executable_candidates=executable_candidates,
    )


def _psutil_listeners_on(
    port: int,
) -> tuple[list[int], bool, list[tuple[str, tuple[object, ...], int | None]]] | None:
    import psutil

    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return None
    rows: list[tuple[str, tuple[object, ...], int | None]] = [
        (conn.status, tuple(conn.laddr), conn.pid) for conn in conns
    ]
    pids: list[int] = []
    for status, addr, pid in rows:
        if status == "LISTEN" and len(addr) >= 2 and addr[1] == port:
            if pid is None:
                return [], False, rows
            pids.append(pid)
    return list(dict.fromkeys(pids)), True, rows


def _lsof_listener_output(port: int, *, fields: str) -> str:
    import subprocess

    argv = lsof_argv("-nP", fields, "-sTCP:LISTEN", f"-iTCP:{port}")
    if argv is None:
        raise ListenerDiscoveryError(
            f"listener discovery failed on port {port}: lsof is not on PATH "
            "and not in the standard locations"
        )
    try:
        out = base.host.proc.run_bounded(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ListenerDiscoveryError(f"listener discovery failed on port {port}: {exc}") from exc
    parse_lsof_pids(port, out.returncode, out.stdout, out.stderr)
    return out.stdout


def strict_listeners_on(port: int) -> list[int]:
    scan = _psutil_listeners_on(port)
    if scan is not None and scan[1]:
        return scan[0]
    lsof_pids = parse_lsof_pids(port, 0, _lsof_listener_output(port, fields="-Fp"), "")
    return reconcile_strict_listeners(port, scan[2] if scan is not None else None, lsof_pids)


def listener_addrs(port: int) -> set[str]:
    import psutil

    try:
        conns = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return _lsof_listener_name_addrs(port)
    rows = [(conn.status, tuple(conn.laddr), conn.pid) for conn in conns]
    return parse_psutil_listener_addrs(port, rows)


def _lsof_listener_name_addrs(port: int) -> set[str]:
    import subprocess

    argv = lsof_argv("-nP", "-Fpn", "-sTCP:LISTEN", f"-iTCP:{port}")
    if argv is None:
        return set()
    try:
        out = base.host.proc.run_bounded(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return parse_lsof_listener_addrs(port, out.returncode, out.stdout)


def serves_off_box(*, machine: MachineIdentityReader) -> bool:
    return machine.host() not in LOOPBACK_ALIASES


def bind_addrs(*, machine: MachineIdentityReader) -> list[str]:
    out = ["127.0.0.1"]
    host = machine.host()
    if host not in LOOPBACK_ALIASES:
        out.append(host)
    return out


def verify_listener_custody(
    port: int,
    *,
    has_identity: bool,
    owner_live: bool,
    owned_pids: Collection[int],
    listener_pids: Collection[int],
    required: bool = True,
) -> frozenset[int]:
    listeners = frozenset(listener_pids)
    if not has_identity:
        if listeners or required:
            raise RuntimeError(f"no owned data-plane process for listener :{port}")
        return listeners
    if required and not listeners:
        raise RuntimeError(f"recorded data-plane process has no listener :{port}; custody retained")
    if not owner_live or not listeners <= set(owned_pids):
        raise RuntimeError(f"data-plane listener :{port} does not belong to the recorded process")
    return listeners


def require_listener(
    identity: OwnedProcess | None, port: int, *, required: bool = True
) -> frozenset[int]:
    listeners = strict_listeners_on(port)
    if identity is None:
        return verify_listener_custody(
            port,
            has_identity=False,
            owner_live=False,
            owned_pids=frozenset(),
            listener_pids=listeners,
            required=required,
        )
    owned = {process.pid for process in capture_tree(identity)}
    return verify_listener_custody(
        port,
        has_identity=True,
        owner_live=identity.live(),
        owned_pids=owned,
        listener_pids=listeners,
        required=required,
    )


def resolve_tcp_bind(bind: TcpBind) -> TcpBind:
    try:
        host = normalize_bind_ip(bind.host)
    except ValueError:
        addresses = socket.getaddrinfo(bind.host, bind.port, type=socket.SOCK_STREAM)
        ipv4 = [row for row in addresses if row[0] == socket.AF_INET]
        host = normalize_bind_ip(str((ipv4 or addresses)[0][4][0]))
    return TcpBind(host, bind.port)


def _available_ipv4_bind(bind: TcpBind) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((bind.host, bind.port))
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            return False
        return True


def resolve_go_tcp_bind(bind: TcpBind) -> TcpBind:
    resolved = resolve_tcp_bind(bind)
    if resolved.host == "0.0.0.0" and socket.has_dualstack_ipv6():  # noqa: S104
        return TcpBind("::", resolved.port)
    return resolved


def _socket_table_matches(
    bind: TcpBind, rows: list[tuple[str, tuple[object, ...], int | None]]
) -> list[tuple[TcpBind, int | None]]:
    matches: list[tuple[TcpBind, int | None]] = []
    for status, addr, pid in rows:
        if status != "LISTEN" or len(addr) < 2 or addr[1] != bind.port:
            continue
        if not isinstance(addr[0], str):
            raise ListenerDiscoveryError(f"invalid listener address for {bind}")
        endpoint = TcpBind(normalize_bind_ip(addr[0]), bind.port)
        if bind.overlaps(endpoint):
            matches.append((endpoint, pid))
    return matches


def _attributed_matches(
    bind: TcpBind, candidates: list[tuple[TcpBind, int | None]], *, fallback: bool
) -> list[tuple[TcpBind, int]]:
    known = [(endpoint, pid) for endpoint, pid in candidates if pid is not None]
    if not fallback and len(known) == len(candidates):
        return known
    reported = lsof_bound_sockets(bind.port, _lsof_listener_output(bind.port, fields="-Fpftn"))
    matches = [(endpoint, pid) for endpoint, pid in reported if bind.overlaps(endpoint)]
    uncovered = {endpoint for endpoint, _ in candidates} - {endpoint for endpoint, _ in matches}
    if uncovered:
        raise ListenerDiscoveryError(f"cannot attribute a listener overlapping {bind}")
    return matches


def listeners_at(bind: TcpBind) -> list[tuple[TcpBind, int]]:
    scan = _psutil_listeners_on(bind.port)
    candidates = _socket_table_matches(bind, scan[2]) if scan is not None else []
    known = _attributed_matches(bind, candidates, fallback=scan is None)
    if ":" in bind.host or all(endpoint.host != "::" for endpoint, _ in known):
        return known
    ipv4 = [(endpoint, pid) for endpoint, pid in known if ":" not in endpoint.host]
    return ipv4 if ipv4 or _available_ipv4_bind(bind) else known


def lsof_bound_sockets(port: int, stdout: str) -> list[tuple[TcpBind, int]]:
    result: list[tuple[TcpBind, int]] = []
    pid: int | None = None
    family = ""
    for line in stdout.splitlines():
        match line[:1]:
            case "p":
                pid = int(line[1:])
            case "f":
                family = ""
            case "t":
                family = line[1:]
            case "n" if pid is not None:
                host, _, port_text = line[1:].rpartition(":")
                if port_text == str(port):
                    if host == "*":
                        host = "::" if family == "IPv6" else "0.0.0.0"  # noqa: S104
                    result.append((TcpBind(normalize_bind_ip(host), port), pid))
            case _:
                continue
    return result


def normalize_bind_ip(host: str) -> str:
    address = ip_address(host.removeprefix("[").removesuffix("]"))
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)
