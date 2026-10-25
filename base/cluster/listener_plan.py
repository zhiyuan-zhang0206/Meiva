from __future__ import annotations

from collections.abc import Collection, Sequence
from collections.abc import Set as AbstractSet


class ListenerDiscoveryError(RuntimeError):
    pass


# meiva:note
# Restricted contexts (cron, forced ssh commands, minimal login shells)
# carry a PATH that often omits the platform's lsof location — macOS keeps
# lsof in /usr/sbin, which cron's /usr/bin:/bin PATH never reaches — so PATH
# lookup alone cannot be trusted. PATH wins when it resolves (fast, and it
# honours an operator's own binary), then these candidates, in order.
LSOF_CANDIDATE_PATHS = (
    "/usr/sbin/lsof",
    "/usr/bin/lsof",
    "/sbin/lsof",
    "/bin/lsof",
    "/usr/local/bin/lsof",
    "/usr/local/sbin/lsof",
    "/opt/homebrew/bin/lsof",
)

LOOPBACK_ALIASES = frozenset({"127.0.0.1", "::1", "localhost", "ip6-localhost"})


def resolve_lsof_argv(
    *args: str,
    path_binary: str | None,
    executable_candidates: Collection[str],
) -> list[str] | None:
    binary = path_binary
    if binary is None:
        for candidate in LSOF_CANDIDATE_PATHS:
            if candidate in executable_candidates:
                binary = candidate
                break
    if binary is None:
        return None
    return [binary, *args]


def _parse_psutil_listeners(
    port: int,
    connections: Sequence[tuple[str, Sequence[object], int | None]],
) -> tuple[list[int], bool]:
    pids: list[int] = []
    for status, addr, pid in connections:
        if status != "LISTEN":
            continue
        if len(addr) < 2:
            continue
        if addr[1] == port:
            if pid is None:
                return [], False
            pids.append(pid)
    return list(dict.fromkeys(pids)), True


def parse_psutil_listener_addrs(
    port: int,
    connections: Sequence[tuple[str, Sequence[object], int | None]],
) -> set[str]:
    addrs: set[str] = set()
    for status, addr, _pid in connections:
        if status != "LISTEN" or len(addr) < 2:
            continue
        if addr[1] == port and isinstance(addr[0], str):
            addrs.add(addr[0])
    return addrs


def parse_lsof_pids(port: int, returncode: int, stdout: str, stderr: str) -> list[int]:
    if returncode not in (0, 1) or stderr.strip() or (returncode == 1 and stdout):
        raise ListenerDiscoveryError(
            f"listener discovery failed on port {port}: lsof exit {returncode}: {stderr.strip()}"
        )
    pids: list[int] = []
    for line in stdout.splitlines():
        if line.startswith("p"):
            try:
                pids.append(int(line[1:]))
            except ValueError:
                continue
    return list(dict.fromkeys(pids))


def parse_lsof_listener_addrs(port: int, returncode: int, stdout: str) -> set[str]:
    if returncode != 0:
        return set()
    addrs: set[str] = set()
    for line in stdout.splitlines():
        if not line.startswith("n"):
            continue
        host, separator, port_text = line[1:].rpartition(":")
        if not separator:
            continue
        try:
            if int(port_text) != port:
                continue
        except ValueError:
            continue
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if host:
            addrs.add(host)
    return addrs


def reconcile_strict_listeners(
    port: int,
    psutil_connections: Sequence[tuple[str, Sequence[object], int | None]] | None,
    lsof_pids: Sequence[int],
) -> list[int]:
    psutil_scan = (
        _parse_psutil_listeners(port, psutil_connections)
        if psutil_connections is not None
        else None
    )
    if psutil_scan is not None and psutil_scan[1]:
        return list(psutil_scan[0])
    if psutil_scan is not None and not lsof_pids:
        raise ListenerDiscoveryError(
            f"listener discovery failed on port {port}: the socket table shows "
            "a listener that neither psutil nor lsof could attribute to a pid"
        )
    return list(lsof_pids)


# meiva:note
# Wildcard bind addresses (`0.0.0.0`, `::`, `*`) in the socket table cover
# every host interface, including the machine's declared reachable address.
def public_listener_bound(
    bind_addresses: Sequence[str],
    reachable_host: str,
    listener_addresses: AbstractSet[str],
) -> bool:
    if list(bind_addresses) == ["127.0.0.1"]:
        return True
    if not listener_addresses:
        return False
    wildcard = bool(listener_addresses & {"0.0.0.0", "::", "*"})  # noqa: S104
    return wildcard or reachable_host in listener_addresses
