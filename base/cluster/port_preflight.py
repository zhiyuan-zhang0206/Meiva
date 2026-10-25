from __future__ import annotations

import os
import shutil
from collections.abc import Collection

import base.host.proc
from base.cluster.listener_plan import (
    LOOPBACK_ALIASES,
    LSOF_CANDIDATE_PATHS,
    ListenerDiscoveryError,
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


def _lsof_listeners_on(port: int) -> list[int]:
    import subprocess

    argv = lsof_argv("-nP", "-Fp", "-sTCP:LISTEN", f"-iTCP:{port}")
    if argv is None:
        raise ListenerDiscoveryError(
            f"listener discovery failed on port {port}: lsof is not on PATH "
            "and not in the standard locations"
        )
    try:
        out = base.host.proc.run_bounded(argv, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ListenerDiscoveryError(f"listener discovery failed on port {port}: {exc}") from exc
    return parse_lsof_pids(port, out.returncode, out.stdout, out.stderr)


def strict_listeners_on(port: int) -> list[int]:
    scan = _psutil_listeners_on(port)
    if scan is not None and scan[1]:
        return scan[0]
    lsof_pids = _lsof_listeners_on(port)
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
        if listeners:
            raise RuntimeError(f"port {port} has listener(s) without a native launch receipt")
        if required:
            raise RuntimeError(f"no native launch receipt owns port {port}")
        return frozenset()
    if not owner_live:
        if listeners:
            raise RuntimeError(f"port {port} has listener(s) after the native owner exited")
        if required:
            raise RuntimeError(f"native owner for port {port} is not live")
        return frozenset()
    for pid in listeners:
        if pid not in owned_pids:
            raise RuntimeError(f"listener pid {pid} on port {port} does not belong to native owner")
    if required and not listeners:
        raise RuntimeError(f"native owner has no listener on port {port}")
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
