# meiva:note
# POST /api/agents/{id}/reply-suggestion through the real gateway app.

from __future__ import annotations

import psycopg
import pytest

from base.db import Database
from base.lm.catalog import ModelCatalog
from gateway.agents.reply_suggestion.tests.support import Harness, useful_reply
from gateway.tests.auth.gateway_config import gateway_test_client


@pytest.fixture
def harness(
    db_conn: psycopg.Connection, database: Database, model_catalog: ModelCatalog
) -> Harness:
    return Harness(db_conn, database, model_catalog)


# meiva:note
# Ask the gateway for a suggestion; its service answers with the harness's model doubles.
def post(harness: Harness, agent_id: int | str | None = None, **fields: object):
    with gateway_test_client(fields=fields, catalog=harness.catalog) as client:  # pyright: ignore[reportArgumentType]
        client.app.state.reply_suggestions = harness.service
        target = harness.agent_id if agent_id is None else agent_id
        return client.post(f"/api/agents/{target}/reply-suggestion")


def test_the_route_returns_the_services_suggestion(harness: Harness) -> None:
    response = post(harness)

    assert response.status_code == 200
    assert response.json() == {
        "suggestions": ["Yes, go ahead.", "Wait, explain the plan first."],
        "reason": None,
    }
    assert len(harness.decide.calls) == 1


def test_the_route_reports_why_there_is_no_suggestion_as_a_200(harness: Harness) -> None:
    harness.decide.result = useful_reply(0.1)

    response = post(harness)

    assert response.status_code == 200
    assert response.json() == {"suggestions": [], "reason": "not_useful"}


def test_the_switch_in_the_gateways_own_config_turns_the_route_off(harness: Harness) -> None:
    response = post(harness, reply_suggestion_enabled=False)

    assert response.status_code == 200
    assert response.json() == {"suggestions": [], "reason": "disabled"}
    assert harness.decide.calls == []


def test_bad_model_configuration_is_an_explicit_capability_failure(harness: Harness) -> None:
    response = post(harness, reply_suggestion_gate_model="deepseek-flash")

    assert response.status_code == 503
    assert response.json()["code"] == "reply_suggestion_unavailable"
    assert response.json()["retryable"] is False
    assert "deepseek-flash" not in response.text
    assert harness.decide.calls == []


def test_a_non_integer_agent_id_is_rejected(harness: Harness) -> None:
    assert post(harness, "abc").status_code == 422


def test_the_route_does_not_swallow_an_unknown_agent(harness: Harness) -> None:
    response = post(harness, 404404)

    assert response.status_code == 404
    assert response.json()["reason"] == "agent_not_found"
