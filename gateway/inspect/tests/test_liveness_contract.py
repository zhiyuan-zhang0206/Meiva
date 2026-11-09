# meiva:note
# The inspector's agents_meta read, against rows in the real database.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from base.api_contracts.agent_contract import LivenessState
from base.db import Database
from base.telemetry.metrics.observed_metrics import MetricObservation, write_observations
from gateway.inspect._live import db_rows_blocking
from gateway.inspect.tests.rows import returned_id


def _agent(conn: psycopg.Connection, *, liveness_state: str, harness: str = "meiva") -> int:
    agent_id = returned_id(conn, "INSERT INTO agents(label) VALUES ('liveness') RETURNING id")
    _ = conn.execute(
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


# meiva:note
# An agent's llm_model and reasoning_effort come from its own config (config_overlay over
# birth_config) regardless of harness, and are never overwritten by telemetry observations
# in agent_metric_observations (such as decision-model or subscription usage rows).
@pytest.mark.parametrize("harness", ["meiva", "codex", "claude_code", "antigravity"])
def test_inspector_resolves_model_and_effort_from_config_not_telemetry_observations(
    harness: str, db_conn: psycopg.Connection, database: Database
) -> None:
    aid = _agent(db_conn, liveness_state="online", harness=harness)
    now = datetime.now(UTC)
    _ = db_conn.execute(
        'UPDATE agents_meta SET birth_config = \'{"llm_model": "gpt-6.1-sol", "reasoning_effort": "high"}\', '
        "config_overlay = '{}' WHERE id = %s",
        (aid,),
    )
    _ = write_observations(
        [
            MetricObservation(
                1,
                aid,
                now,
                "usage",
                model="jev-1.13.0",
                usage_calls=1,
                cost_usd=Decimal("0.00003"),
            ),
            MetricObservation(
                2,
                aid,
                now + timedelta(seconds=1),
                "usage",
                model="gpt-5.4",
                usage_calls=1,
                cost_usd=Decimal("0.05"),
                cost_usd_subscription=Decimal("0.05"),
                unpriced_calls=1,
                unpriced_calls_subscription=1,
            ),
        ],
        db=db_conn,
    )
    db_conn.commit()
    with database.pool(max_size=1) as pool:
        live = db_rows_blocking(pool, aid)
        assert live.config_overlay["llm_model"] == "gpt-6.1-sol"
        assert live.config_overlay["reasoning_effort"] == "high"

        _ = db_conn.execute(
            'UPDATE agents_meta SET config_overlay = \'{"llm_model": "claude-opus-4-6", "reasoning_effort": "medium"}\' '
            "WHERE id = %s",
            (aid,),
        )
        db_conn.commit()
        updated = db_rows_blocking(pool, aid)
        assert updated.config_overlay["llm_model"] == "claude-opus-4-6"
        assert updated.config_overlay["reasoning_effort"] == "medium"


# meiva:note
# An agent without config pins has no llm_model synthesized from telemetry observations.
@pytest.mark.parametrize("harness", ["meiva", "codex"])
def test_agent_without_config_pins_keeps_empty_overlay(
    harness: str, db_conn: psycopg.Connection, database: Database
) -> None:
    aid = _agent(db_conn, liveness_state="online", harness=harness)
    _ = write_observations(
        [
            MetricObservation(
                1,
                aid,
                datetime.now(UTC),
                "usage",
                model="gpt-5.4",
                usage_calls=1,
                unpriced_calls=1,
                unpriced_calls_subscription=1,
            )
        ],
        db=db_conn,
    )
    db_conn.commit()
    with database.pool(max_size=1) as pool:
        assert "llm_model" not in db_rows_blocking(pool, aid).config_overlay


# meiva:note
# When config_overlay omits llm_model, the inspector surfaces birth_config's llm_model in overlay.
def test_inspector_surfaces_birth_config_model_when_overlay_omitted(
    db_conn: psycopg.Connection, database: Database
) -> None:
    aid = _agent(db_conn, liveness_state="online")
    _ = db_conn.execute(
        'UPDATE agents_meta SET birth_config = \'{"llm_model": "deepseek-flash"}\', '
        "config_overlay = '{}' WHERE id = %s",
        (aid,),
    )
    db_conn.commit()
    with database.pool(max_size=1) as pool:
        assert db_rows_blocking(pool, aid).config_overlay["llm_model"] == "deepseek-flash"
