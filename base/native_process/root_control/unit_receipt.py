from __future__ import annotations

import json
from pathlib import Path

from base.native_process.kernel_identity import native_boot_id
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.client import RootClientError, native_identity
from base.native_process.untyped import is_object_dict, object_of


def read_identity(path: Path) -> OwnedProcess | None:
    try:
        record = object_of(json.loads, path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    if not is_object_dict(record) or record.get("boot_id") != native_boot_id():
        return None
    try:
        identity = native_identity(record)
        _ = identity.birth_key()
        return identity
    except (RootClientError, RuntimeError, ValueError):
        return None


def encode_identity(process: OwnedProcess) -> str:
    record = {
        "boot_id": native_boot_id(),
        "pid": process.pid,
        "create_time": process.birth,
        "starttime": process.starttime,
    }
    return json.dumps(record) + "\n"
