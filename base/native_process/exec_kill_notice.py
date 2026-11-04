"""The host's note, left beside an exec run's result file, on why it killed that exec.

The agent host's memory guard writes it before killing the exec's session leader;
the run that owns the exec reads (and consumes) it once the root has exited, so
the agent's result names the cause instead of a missing result envelope. Both
sides know only the result path, so the note travels as a file.
"""

from __future__ import annotations

import json
from pathlib import Path

from base.native_process.untyped import is_object_dict, object_of


def notice_path(result_path: Path) -> Path:
    """Where the host records why it killed the exec writing `result_path`."""
    return result_path.with_name(result_path.name + ".host-kill")


def write_notice(result_path: Path, notice: str) -> None:
    notice_path(result_path).write_text(json.dumps({"notice": notice}), encoding="utf-8")


def read_notice(result_path: Path) -> str | None:
    """The host's kill reason for this run, consumed so it never outlives the run."""
    path = notice_path(result_path)
    try:
        body = object_of(json.loads, path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not is_object_dict(body):
        raise TypeError(f"{path}: a kill notice is a JSON object")
    text = str(body["notice"])
    path.unlink(missing_ok=True)
    return text
