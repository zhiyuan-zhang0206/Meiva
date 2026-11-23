# meiva:link services/supervision/meiva_root/docs/meiva_root.md

from __future__ import annotations

import logging
from pathlib import Path

from base.host.atomic_io import write_text_atomic
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.unit_receipt import encode_identity, read_identity
from base.paths import root_unit_pidfile as pidfile_path

_log = logging.getLogger(__name__)


def write(run_dir: Path, unit_id: str, identity: OwnedProcess) -> None:
    try:
        path = pidfile_path(run_dir, unit_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(path, encode_identity(identity), mode=0o600, sync_parent=True)
    except OSError:
        _log.exception("unit %s: could not write process receipt", unit_id)


def read(run_dir: Path, unit_id: str) -> OwnedProcess | None:
    path = pidfile_path(run_dir, unit_id)
    identity = read_identity(path)
    if identity is None:
        path.unlink(missing_ok=True)
    return identity


def clear(run_dir: Path, unit_id: str, *, identity: OwnedProcess | None = None) -> None:
    path = pidfile_path(run_dir, unit_id)
    if identity is not None and read_identity(path) != identity:
        return
    path.unlink(missing_ok=True)
