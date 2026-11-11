from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from base.host.boundary.jsonl import read_jsonl_rows
from base.lm.harness.claude.reconciliation.judge import judge
from base.lm.harness.delivery import Reconciliation, home_in


# meiva:note
# The read half of Claude reconciliation: find the session transcript under the injected Claude home
# (`<home>/projects/<project slug>/<session id>.jsonl`; the session id is unique, so the slug is not
# needed), parse its lines and hand the rows to the pure `judge`. A missing transcript answers None
# (there is no record to ask) and a malformed line is an error, because skipping it could turn a
# recorded input into a re-delivery.
# meiva:link base/lm/harness/claude/docs/claude.md
def claude_home(config_dir: Path | None, environ: Mapping[str, str]) -> Path:
    if config_dir is not None:
        return config_dir
    return Path(home_in(environ)) / ".claude"


def transcript_path(claude_home: Path, session_id: str) -> Path | None:
    matches = sorted((claude_home / "projects").glob(f"*/{session_id}.jsonl"))
    return matches[-1] if matches else None


def reconcile_inputs(
    claude_home: Path, session_id: str, keys: Sequence[str]
) -> dict[str, Reconciliation] | None:
    path = transcript_path(claude_home, session_id)
    if path is None:
        return None
    rows = read_jsonl_rows(path)
    return {key: judge(rows, key) for key in keys}
