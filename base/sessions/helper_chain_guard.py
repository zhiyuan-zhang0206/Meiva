"""Detect long-lived processes orphaned from the permissions helper."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

import psutil

HELPER_PID_ENV = "MEIVA_PERMISSIONS_HELPER_PID"

_MAX_ANCESTOR_HOPS = 32
"""Upper bound on the ancestor walk (ppid reads, this process excluded).

The helper spawns a unit as a direct child, and everything that must stay
anchored sits a handful of hops below that unit (the root supervisor, then
child daemons). 32 is generous headroom for that geometry and still
terminates on a circular or otherwise pathological ppid chain.
"""


def _read_ppid(pid: int) -> int | None:
    """The ppid of `pid`, or None when the chain cannot be followed further.

    None means the process is gone or not visible — a broken link, never a
    match.
    """
    try:
        return psutil.Process(pid).ppid()
    except psutil.Error:
        return None


def helper_marker_pid(environ: Mapping[str, str]) -> str | None:
    return environ.get(HELPER_PID_ENV)


# meiva:note
# Whether this process is still anchored to the helper that spawned it.
# Processes not spawned by the helper carry no marker and are left alone.
# A malformed injected marker is treated as a broken chain rather than
# silently disabling the guard. A marked process is intact while the
# helper appears anywhere on its ancestor chain — the direct child the
# helper spawned, and every descendant of it (the root supervisor, a
# child daemon) — within `_MAX_ANCESTOR_HOPS` ppid hops; a link that
# cannot be read (an intermediate process is gone) breaks the chain.
# `ppids` overrides the process table when tests feed a synthetic chain.
def parent_chain_intact(
    pid: int, *, environ: Mapping[str, str], ppids: Mapping[int, int] = MappingProxyType({})
) -> bool:
    raw_helper_pid = helper_marker_pid(environ)
    if raw_helper_pid is None:
        return True
    try:
        helper_pid = int(raw_helper_pid)
    except ValueError:
        return False
    if helper_pid <= 0:
        return False
    return _helper_on_ancestor_chain(pid, helper_pid, ppids)


def _helper_on_ancestor_chain(pid: int, helper_pid: int, ppids: Mapping[int, int]) -> bool:
    for _ in range(_MAX_ANCESTOR_HOPS):
        ppid = ppids.get(pid) if ppids else _read_ppid(pid)
        if ppid is None:
            return False
        if ppid == helper_pid:
            return True
        if ppid <= 1:
            return False
        pid = ppid
    return False
