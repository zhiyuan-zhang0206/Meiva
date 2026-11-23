# meiva:link services/supervision/meiva_root/docs/meiva_root.md

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from base.host.atomic_io import write_text_atomic
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.unit_receipt import (
    decode_identity,
    encode_identity,
    read_identity,
)
from base.paths import root_unit_pidfile as pidfile_path

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LegacyPid:
    pid: int


def write(run_dir: Path, unit_id: str, identity: OwnedProcess) -> None:
    try:
        path = pidfile_path(run_dir, unit_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, encode_identity(identity), mode=0o600, sync_parent=True)
    except OSError:
        _log.exception("unit %s: could not write process receipt", unit_id)


def read(run_dir: Path, unit_id: str) -> OwnedProcess | LegacyPid | None:
    path = pidfile_path(run_dir, unit_id)
    try:
        contents = path.read_text()
    except FileNotFoundError:
        return None
    except UnicodeError:
        contents = ""
    pid = contents.strip()
    if pid.isascii() and pid.isdecimal() and len(pid) <= 10:
        value = int(pid)
        if 0 < value <= 2**31 - 1:
            return LegacyPid(value)
    identity = decode_identity(contents)
    if identity is None:
        path.unlink(missing_ok=True)
    return identity


def clear(run_dir: Path, unit_id: str, *, identity: OwnedProcess | LegacyPid | None = None) -> None:
    path = pidfile_path(run_dir, unit_id)
    if isinstance(identity, OwnedProcess) and read_identity(path) != identity:
        return
    if isinstance(identity, LegacyPid) and read(run_dir, unit_id) != identity:
        return
    path.unlink(missing_ok=True)
