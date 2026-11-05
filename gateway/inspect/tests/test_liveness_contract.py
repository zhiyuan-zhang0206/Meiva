# meiva:note
# The inspector's agents_meta read, against rows in the real database.

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from base.api_contracts.agent_contract import LivenessState
from base.db import Database
from base.telemetry.metrics.observed_metrics import MetricObservation, write_observations
from gateway.inspect._live import db_rows_blocking
from gateway.inspect.tests.rows import returned_id


def _agent(conn: psycopg.Connection, *, liveness_state: str, harness: str = "meiva") -> int:
    agent_id = returned_id(conn, "INSERT INTO agents(label) VALUES ('liveness') RETURNING id")
    conn.execute(
        "INSERT INTO agents_meta(id, spawner, status, machine, liveness_state, harness) "
        "VALUES (%s, 'user', 'idling', 'machine', %s, %s)",
        (agent_id, liveness_state, harness),
    )
    conn.commit()
    return agent_id


@pytest.mark.parametrize("state", ["online", "offline", "unknown"])
def test_inspector_parses_liveness_at_db_boundary(
    state: str, db_conn: psycopg.Connection, database: Database
) -> None:
    aid = _agent(db_conn, liveness_state=state)
    with database.pool(max_size=1) as pool:
        assert db_rows_blocking(pool, aid).liveness_state is LivenessState(state)


def test_inspector_populates_observed_model_for_external_harness(
    db_conn: psycopg.Connection, database: Database
) -> None:
    aid = _agent(db_conn, liveness_state="online", harness="codex")
    write_observations(
        [MetricObservation(1, aid, datetime.now(UTC), "usage", model="gpt-5.4", usage_calls=1)],
        db=db_conn,
    )
    db_conn.commit()
    with database.pool(max_size=1) as pool:
        assert db_rows_blocking(pool, aid).config_overlay["llm_model"] == "gpt-5.4"


def test_inspector_ignores_usage_from_before_the_harness_transition(
    db_conn: psycopg.Connection, database: Database
) -> None:
    aid = _agent(db_conn, liveness_state="online", harness="claude_code")
    switched_at = datetime.now(UTC)
    db_conn.execute(
        "UPDATE agents_meta SET harness_configured_at = %s, "
        'config_overlay = \'{"llm_model": "sonnet"}\' WHERE id = %s',
        (switched_at, aid),
    )
    write_observations(
        [MetricObservation(1, aid, switched_at - timedelta(seconds=1), "usage", model="gpt-5.4")],
        db=db_conn,
    )
    db_conn.commit()
    with database.pool(max_size=1) as pool:
        assert db_rows_blocking(pool, aid).config_overlay["llm_model"] == "sonnet"
        write_observations(
            [MetricObservation(2, aid, switched_at, "usage", model="claude-sonnet-5-5")],
            db=db_conn,
        )
        db_conn.commit()
        assert db_rows_blocking(pool, aid).config_overlay["llm_model"] == "claude-sonnet-5-5"


# meiva:note
# Only an external harness reports the model it ran; a Meiva agent's model is its overlay.
def test_a_meiva_agent_keeps_its_own_model_choice(
    db_conn: psycopg.Connection, database: Database
) -> None:
    aid = _agent(db_conn, liveness_state="online")
    write_observations(
        [MetricObservation(1, aid, datetime.now(UTC), "usage", model="gpt-5.4", usage_calls=1)],
        db=db_conn,
    )
    db_conn.commit()
    with database.pool(max_size=1) as pool:
        assert "llm_model" not in db_rows_blocking(pool, aid).config_overlay
