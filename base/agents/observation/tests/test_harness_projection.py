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
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (41)")
        cur.execute(
            "INSERT INTO agents_meta (id, status, harness) VALUES (41, 'idling', %s)",
            (harness.value,),
        )
    detail = select_one(db_conn, 41, catalog=None, default_model="deepseek-v4-flash")
    roster = select_roster(db_conn, catalog=None, default_model="deepseek-v4-flash")
    directory = list_directory(db_conn, catalog=None, default_model="deepseek-v4-flash")
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


def test_snapshot_reports_harness_exit_and_last_turn_fatal_reason(
    db_conn: psycopg.Connection,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (42)")
        cur.execute(
            "INSERT INTO agents_meta "
            "(id, status, harness, harness_exit_code, harness_exit_at, "
            "last_turn_fatal_at, last_turn_fatal_reason) "
            "VALUES (42, 'terminated', 'claude_code', 1, now(), now(), "
            "'FatalLLMStreamError: proxy connect refused')"
        )
    detail = select_one(db_conn, 42, catalog=None, default_model="deepseek-v4-flash")
    assert detail is not None
    assert detail.harness_exit_code == 1
    assert detail.harness_exit_at is not None
    assert detail.last_turn_fatal_at is not None
    assert detail.last_turn_fatal_reason == "FatalLLMStreamError: proxy connect refused"


def test_snapshot_reports_the_recovery_halt(db_conn: psycopg.Connection) -> None:
    with db_conn.cursor() as cur:
        cur.execute("INSERT INTO agents (id) VALUES (43)")
        cur.execute(
            "INSERT INTO agents_meta "
            "(id, status, harness, permanent_reject_streak, last_permanent_reject_reason) "
            "VALUES (43, 'idling', 'claude_code', 1, 'quota_exhausted')"
        )
    detail = select_one(db_conn, 43, catalog=None, default_model="deepseek-v4-flash")
    assert detail is not None
    assert (detail.permanent_reject_streak, detail.last_permanent_reject_reason) == (
        1,
        "quota_exhausted",
    )
