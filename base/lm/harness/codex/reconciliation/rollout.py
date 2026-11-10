from __future__ import annotations

import json
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness.codex.reconciliation.judge import judge
from base.lm.harness.delivery import Reconciliation


# meiva:note
# The read half of Codex reconciliation: find the thread's rollout under the injected Codex home
# (`<home>/sessions/YYYY/MM/DD/rollout-*<thread id>.jsonl`), parse its lines, and hand the rows to the
# pure `judge`. The home is a parameter because it is the one fact that differs per machine and per
# test; nothing here looks at the environment. A missing rollout is not "not found" for an input: the
# caller learns there is no record to ask, and a malformed line is an error rather than a skipped row,
# because skipping it could turn a recorded input into a re-delivery.
# meiva:link base/lm/harness/codex/docs/codex.md
def rollout_path(codex_home: Path, thread_id: str) -> Path | None:
    matches = sorted((codex_home / "sessions").glob(f"*/*/*/rollout-*{thread_id}.jsonl"))
    return matches[-1] if matches else None


def reconcile_input(codex_home: Path, thread_id: str, key: str) -> Reconciliation | None:
    path = rollout_path(codex_home, thread_id)
    if path is None:
        return None
    rows: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        parsed = object_of(json.loads, line)
        if not is_object_dict(parsed):
            raise ValueError(f"{path}:{number}: a rollout row is not an object")
        rows.append(parsed)
    return judge(rows, key)
