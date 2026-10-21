from __future__ import annotations

import psycopg
import pytest
from pydantic import ValidationError

from base.agents.observation.roster import list_directory, select_roster
from base.agents.observation.snapshot import select_one
from base.api_contracts.agent_contract import Harness


@pytest.mark.parametrize("harness", [Harness.MEIVA, Harness.CLAUDE_CODE, Harness.CODEX])
def test_agent_views_preserve_the_stored_harness(
    db_conn: psycopg.Connection, harness: Harness
) -> None:
    def unused_model() -> str:
        raise AssertionError("Harness identity does not need the model catalog")

    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (41)")
        cur.execute(
            "INSERT INTO agents_meta (id, status, harness) VALUES (41, 'idling', %s)",
            (harness.value,),
        )
    detail = select_one(db_conn, 41, catalog=None, default_model_reader=unused_model)
    roster = select_roster(db_conn, catalog=None, default_model_reader=unused_model)
    directory = list_directory(db_conn, catalog=None, default_model_reader=unused_model)
    assert detail is not None
    for view in (detail, roster.agents[0], directory.agents[0]):
        assert view.harness is harness
        payload = view.model_dump(mode="json")
        assert payload["harness"] == harness.value
        for invalid in (None, "unknown-harness"):
            with pytest.raises(ValidationError):
                type(view).model_validate({**payload, "harness": invalid})
        del payload["harness"]
        with pytest.raises(ValidationError):
            type(view).model_validate(payload)
