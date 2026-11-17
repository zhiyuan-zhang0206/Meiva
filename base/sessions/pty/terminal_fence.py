# meiva:note
# Durable terminal cutoffs for agent-owned PTY names.
#
# A lifecycle caller writes this state before it can acknowledge shell closure.
# The PTY service reloads it before accepting allocations, so a request held by
# a client or proxy across a daemon restart cannot recreate a pre-terminal
# session index after its owner resurrects. The state is monotonic because
# agent ids are never reused.

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from base.host.atomic_io import write_text_atomic
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.native_process.os_platform import LockTimeoutError, file_lock
from base.paths import run_dir

_VERSION = 1
_MAX_ID = (1 << 63) - 1
_FILENAME = "pty-terminal-fences.json"
_LOCK_FILENAME = "pty-terminal-fences.lock"


class TerminalFenceStateError(RuntimeError):
    pass


@dataclass(frozen=True)
class TerminalFences:
    cutoffs: dict[int, int]
    error: str | None = None


def state_path(home: Path) -> Path:
    return run_dir(home) / _FILENAME


def _lock_path(home: Path) -> Path:
    return run_dir(home) / _LOCK_FILENAME


def _invalid(message: str) -> TerminalFences:
    return TerminalFences({}, error=message)


def _parse(raw: object) -> TerminalFences:
    if not is_object_dict(raw):
        return _invalid("terminal fence state root must be an object")
    if raw.get("version") != _VERSION:
        return _invalid(f"terminal fence state version is {raw.get('version')!r}")
    cutoffs = raw.get("cutoffs")
    if not is_object_dict(cutoffs):
        return _invalid("terminal fence state cutoffs must be an object")
    parsed: dict[int, int] = {}
    for raw_agent_id, raw_cutoff in cutoffs.items():
        if not isinstance(raw_agent_id, str) or not raw_agent_id.isdecimal():
            return _invalid("terminal fence agent id must be decimal text")
        agent_id = int(raw_agent_id)
        if not 1 <= agent_id <= _MAX_ID or str(agent_id) != raw_agent_id:
            return _invalid(f"terminal fence agent id is out of range: {raw_agent_id!r}")
        if isinstance(raw_cutoff, bool) or not isinstance(raw_cutoff, int):
            return _invalid(f"terminal fence cutoff for agent {agent_id} must be an integer")
        if not 0 <= raw_cutoff <= _MAX_ID:
            return _invalid(f"terminal fence cutoff for agent {agent_id} is out of range")
        parsed[agent_id] = raw_cutoff
    return TerminalFences(parsed)


# meiva:note
# A missing state means no terminal cutoff has ever been recorded. An unreadable
# or malformed state remains explicit so the PTY service can deny agent-shell
# allocation instead of guessing that no prior terminal transition exists.
def read(home: Path) -> TerminalFences:
    try:
        raw = object_of(json.loads, state_path(home).read_text())
    except FileNotFoundError:
        return TerminalFences({})
    except (OSError, ValueError) as exc:
        return _invalid(f"terminal fence state is unreadable: {exc}")
    return _parse(raw)


def _write(home: Path, cutoffs: dict[int, int]) -> None:
    payload = {
        "version": _VERSION,
        "cutoffs": {str(agent_id): cutoff for agent_id, cutoff in sorted(cutoffs.items())},
    }
    write_text_atomic(
        state_path(home),
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        mode=0o600,
        sync_file=True,
        sync_parent=True,
    )


# meiva:note
# Record the maximum terminal cutoff before a lifecycle caller can drop its TTL
# evidence. The file lock serializes independent lifecycle processes and atomic
# replacement makes every reader see either the older complete state or the new
# complete state, never a partial cutoff map.
def record(home: Path, agent_id: int, before_session_index: int) -> int:
    if isinstance(agent_id, bool) or not 1 <= agent_id <= _MAX_ID:
        raise ValueError(f"agent id out of range: {agent_id!r}")
    if isinstance(before_session_index, bool) or not 0 <= before_session_index <= _MAX_ID:
        raise ValueError(f"terminal cutoff out of range: {before_session_index!r}")
    try:
        with file_lock(_lock_path(home), timeout_s=60.0):
            snapshot = read(home)
            if snapshot.error is not None:
                raise TerminalFenceStateError(snapshot.error)
            cutoffs = dict(snapshot.cutoffs)
            cutoff = max(before_session_index, cutoffs.get(agent_id, 0))
            cutoffs[agent_id] = cutoff
            _write(home, cutoffs)
            return cutoff
    except (LockTimeoutError, OSError) as exc:
        raise TerminalFenceStateError(f"cannot persist terminal fence: {exc}") from exc
