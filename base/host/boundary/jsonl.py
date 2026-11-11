from __future__ import annotations

import json
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

__all__ = ["read_jsonl_rows"]


# meiva:note
# The rows of a CLI's JSONL record, all parsed at once so a batch of inputs is judged with one read.
# A malformed line is an error, because skipping it could turn a recorded input into a re-delivery,
# with one exception: the last line when the file does not end in a newline. A process killed
# mid-write leaves exactly that half line, and a half line records nothing that was complete.
def read_jsonl_rows(path: Path) -> list[dict[str, object]]:
    text = path.read_text()
    lines = text.split("\n")
    unterminated = None if text.endswith("\n") or not text else len(lines)
    rows: list[dict[str, object]] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            parsed = object_of(json.loads, line)
        except json.JSONDecodeError:
            if number == unterminated:
                continue
            raise
        if not is_object_dict(parsed):
            raise ValueError(f"{path}:{number}: a record row is not an object")
        rows.append(parsed)
    return rows
