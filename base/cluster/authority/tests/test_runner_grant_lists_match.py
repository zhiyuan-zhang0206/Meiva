# meiva:note
# The runner's lease-table authority is written down twice: once as the bootstrap `GRANT`s inside
# `db/schema.sql` (what a fresh database grants before any Python runs) and once as the statements
# `grant_event_log_runner_access` executes on every start (what the running cluster converges to).
# The two lists drifted once already — `relay_generation` / `relay_degraded_reason` /
# `relay_degraded_at` were granted at runtime but missing from the bootstrap list, so only the
# runtime path was covered by `test_runner_impersonation_grants.py`. Comparing the two sources here
# keeps a single-column edit from silently landing on one side only: the DDL half of this PR drops
# two names from both, and a mismatch would make a fresh database grant a column that no longer
# exists (the failure class #5375 warned about).

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import psycopg

from base.cluster.authority.event_grants import grant_event_log_runner_access

SCHEMA = Path(__file__).resolve().parents[4] / "db" / "schema.sql"


class _StatementRecorder:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object) -> None:
        render = getattr(statement, "as_string", None)
        self.statements.append(str(render(None)) if callable(render) else str(statement))


def _columns(statement: str, privilege: str) -> set[str]:
    match = re.search(rf"GRANT {privilege} \((.*?)\) ON agent_impersonations", statement, re.S)
    assert match is not None, f"no GRANT {privilege} list on agent_impersonations in: {statement}"
    return {column.strip() for column in match.group(1).split(",") if column.strip()}


def test_runtime_grants_match_the_bootstrap_grants_in_the_schema() -> None:
    recorder = _StatementRecorder()
    grant_event_log_runner_access(cast(psycopg.Connection, recorder), "meiva_runner")

    runtime = {"INSERT": set(), "UPDATE": set()}
    for statement in recorder.statements:
        for privilege in runtime:
            if f"GRANT {privilege} (" in statement and "ON agent_impersonations" in statement:
                runtime[privilege] |= _columns(statement, privilege)

    schema = SCHEMA.read_text()
    bootstrap = {"INSERT": set(), "UPDATE": set()}
    for privilege in bootstrap:
        match = re.search(
            rf"GRANT {privilege} \((.*?)\) ON agent_impersonations TO meiva_runner", schema, re.S
        )
        assert match is not None, f"db/schema.sql has no bootstrap GRANT {privilege} list"
        bootstrap[privilege] = {c.strip() for c in match.group(1).split(",") if c.strip()}

    assert bootstrap == runtime
