# meiva:note
# Standard pidfile tracking for meiva-root supervised units.
#
# Root writes one pidfile per unit right after it spawns that unit's process, and
# clears it when that process exits. The pidfile outlives a root that left its children
# behind, allowing the next root to stop the leftover process before spawning a replacement.
# meiva:link services/supervision/meiva_root/docs/meiva_root.md

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger(__name__)

_DIRECTORY = "units"


# meiva:note
# This unit's pidfile path under `run_dir`.
def pidfile_path(run_dir: Path, unit_id: str) -> Path:
    return run_dir / _DIRECTORY / f"{unit_id}.pid"


record_path = pidfile_path


# meiva:note
# Persist one unit's pidfile atomically.
def write(run_dir: Path, unit_id: str, pid: int) -> None:
    from base.host.atomic_io import write_text_atomic

    path = pidfile_path(run_dir, unit_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, f"{pid}\n", mode=0o644, sync_parent=True)
    except OSError:
        _log.exception("unit %s: could not write pidfile", unit_id)


# meiva:note
# Read the recorded pid of `unit_id`, or None when absent or invalid.
def read(run_dir: Path, unit_id: str) -> int | None:
    path = pidfile_path(run_dir, unit_id)
    try:
        text = path.read_text().strip()
        return int(text)
    except FileNotFoundError:
        return None
    except (ValueError, OSError) as exc:
        _log.warning("unit %s: discarding unreadable pidfile %s (%s)", unit_id, path, exc)
        clear(run_dir, unit_id)
        return None


# meiva:note
# Drop the pidfile — of this unit, or only if current pid matches `pid`.
def clear(run_dir: Path, unit_id: str, *, pid: int | None = None) -> None:
    path = pidfile_path(run_dir, unit_id)
    if pid is not None:
        current = read(run_dir, unit_id)
        if current is not None and current != pid:
            return
    path.unlink(missing_ok=True)
