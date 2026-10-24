# meiva:note
# How the verifier reports its outcomes, as values: the lines or JSON the operator reads and the
# exit status a script branches on.

from __future__ import annotations

import json
from collections.abc import Sequence

from ops.private_files.model import Outcome

__all__ = ["exit_code", "render"]


# meiva:note
# The whole output: one JSON array of outcomes, or one `OK`/`FAIL` line per outcome, newline
# terminated.
def render(outcomes: Sequence[Outcome], *, json_output: bool) -> str:
    if json_output:
        return json.dumps([outcome.as_json() for outcome in outcomes]) + "\n"
    lines: list[str] = []
    for outcome in outcomes:
        if outcome.ok:
            lines.append(f"OK {outcome.entry_id} {outcome.path}\n")
        else:
            lines.append(f"FAIL {outcome.entry_id} {outcome.path}: {'; '.join(outcome.errors)}\n")
    return "".join(lines)


# meiva:note
# Success only when every registered file verified.
def exit_code(outcomes: Sequence[Outcome]) -> int:
    return 0 if all(outcome.ok for outcome in outcomes) else 1
