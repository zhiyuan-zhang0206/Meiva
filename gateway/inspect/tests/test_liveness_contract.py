# meiva:note
# The inspector's agents_meta read, against rows in the real database.

from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from base.api_contracts.agent_contract import LivenessState
from base.db import Database
from base.telemetry.metrics.observed_metrics import MetricObservation, write_observations
from gateway.inspect._live import db_rows_blocking


def _agent(conn: psycopg.Connection, *, liveness_state: str, harness: str = "meiva") -> int:
    row = conn.execute("INSERT INTO agents(label) VALUES ('liveness') RETURNING id").fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO agents_meta(id, spawner, status, machine, liveness_state, harness) "
        "VALUES (%s, 'user', 'idling', 'machine', %s, %s)",
        (row[0], liveness_state, harness),
    )
    conn.commit()
    return row[0]


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
