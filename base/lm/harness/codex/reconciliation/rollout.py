from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from base.host.boundary.jsonl import read_jsonl_rows
from base.lm.harness.codex.reconciliation.judge import judge
from base.lm.harness.delivery import Reconciliation, home_in


# meiva:note
# The read half of Codex reconciliation: find the thread's rollout under the injected Codex home
# (`<home>/sessions/YYYY/MM/DD/rollout-*<thread id>.jsonl`), parse its lines, and hand the rows to the
# pure `judge`. The home is a parameter because it is the one fact that differs per machine and per
# test; nothing here looks at the environment. A missing rollout is not "not found" for an input: the
# caller learns there is no record to ask, and a malformed line is an error rather than a skipped row,
# because skipping it could turn a recorded input into a re-delivery.
# meiva:link base/lm/harness/codex/docs/codex.md
def codex_home(environ: Mapping[str, str]) -> Path:
    configured = environ.get("CODEX_HOME")
    return Path(configured) if configured else Path(home_in(environ)) / ".codex"


def rollout_path(codex_home: Path, thread_id: str) -> Path | None:
    matches = sorted((codex_home / "sessions").glob(f"*/*/*/rollout-*{thread_id}.jsonl"))
    return matches[-1] if matches else None


def reconcile_inputs(
    codex_home: Path, thread_id: str, keys: Sequence[str]
) -> dict[str, Reconciliation] | None:
    path = rollout_path(codex_home, thread_id)
    if path is None:
        return None
    rows = read_jsonl_rows(path)
    return {key: judge(rows, key) for key in keys}
