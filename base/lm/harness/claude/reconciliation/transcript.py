from __future__ import annotations

import json
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness.claude.reconciliation.judge import judge
from base.lm.harness.delivery import Reconciliation


# meiva:note
# The read half of Claude reconciliation: find the session transcript under the injected Claude home
# (`<home>/projects/<project slug>/<session id>.jsonl`; the session id is unique, so the slug is not
# needed), parse its lines and hand the rows to the pure `judge`. A missing transcript answers None
# (there is no record to ask) and a malformed line is an error, because skipping it could turn a
# recorded input into a re-delivery.
# meiva:link base/lm/harness/claude/docs/claude.md
def transcript_path(claude_home: Path, session_id: str) -> Path | None:
    matches = sorted((claude_home / "projects").glob(f"*/{session_id}.jsonl"))
    return matches[-1] if matches else None


def reconcile_input(claude_home: Path, session_id: str, key: str) -> Reconciliation | None:
    path = transcript_path(claude_home, session_id)
    if path is None:
        return None
    rows: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        parsed = object_of(json.loads, line)
        if not is_object_dict(parsed):
            raise ValueError(f"{path}:{number}: a transcript row is not an object")
        rows.append(parsed)
    return judge(rows, key)
