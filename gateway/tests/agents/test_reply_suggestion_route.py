# meiva:note
# POST /api/agents/{id}/reply-suggestion through the real gateway app.

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import psycopg
import pytest

from base.db import Database
from base.host.env.tests.env_files import unit_env_path
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_contract import BuildContext
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records
from base.packages.tests.model_catalog import inert_keys
from gateway.agents.reply_suggestion.tests.provider_wire import DraftReply, GateReply, ProviderWire
from gateway.agents.reply_suggestion.tests.support import Harness
from gateway.tests.auth.gateway_config import gateway_test_client

_KEYS = "TYPESAFE_API_KEY=ts-test-key\nDEEPSEEK_API_KEY=sk-test-key\n"


@pytest.fixture
def make_harness(
    db_conn: psycopg.Connection,
    database: Database,
    model_catalog: ModelCatalog,
    unit_home: Path,
    tmp_path: Path,
) -> Iterator[Callable[[GateReply], Harness]]:
    unit_env_path().write_text(_KEYS)
    with ExitStack() as resources:

        def make(gate: GateReply) -> Harness:
            wire = resources.enter_context(
                ProviderWire(tmp_path / "provider", (gate,), DraftReply())
            )
            return Harness(
                db_conn, database, model_catalog, wire=wire, keys=inert_keys(model_catalog)
            )

        yield make


@pytest.fixture
def harness(make_harness: Callable[[GateReply], Harness]) -> Harness:
    return make_harness(GateReply())


# meiva:note
# Ask the gateway for a suggestion; its service answers through the harness's gate wire and draft chat.
def post(harness: Harness, agent_id: int | str | None = None, **fields: object):
    with gateway_test_client(fields=fields, catalog=harness.catalog) as client:  # pyright: ignore[reportArgumentType]
        target = harness.agent_id if agent_id is None else agent_id
        return client.post(f"/api/agents/{target}/reply-suggestion")


def test_the_route_returns_the_services_suggestion(harness: Harness) -> None:
    response = post(harness)

    assert response.status_code == 200
    body = response.json()
    assert isinstance(body["offer_id"], int) and body["offer_id"] > 0
    assert body == {
        "suggestions": ["Yes, go ahead.", "Wait, explain the plan first."],
        "reason": None,
        "offer_id": body["offer_id"],
    }
    assert len(harness.wire.gate_receipts()) == 1


def test_the_route_reports_why_there_is_no_suggestion_as_a_200(
    make_harness: Callable[[GateReply], Harness],
) -> None:
    harness = make_harness(GateReply(0.1))

    response = post(harness)

    assert response.status_code == 200
    assert response.json() == {"suggestions": [], "reason": "not_useful", "offer_id": None}


def test_the_switch_in_the_gateways_own_config_turns_the_route_off(harness: Harness) -> None:
    response = post(harness, reply_suggestion_enabled=False)

    assert response.status_code == 200
    assert response.json() == {"suggestions": [], "reason": "disabled", "offer_id": None}
    assert harness.wire.gate_receipts() == []


def test_bad_model_configuration_is_an_explicit_capability_failure(harness: Harness) -> None:
    response = post(harness, reply_suggestion_gate_model="deepseek-flash")

    assert response.status_code == 503
    assert response.json()["code"] == "reply_suggestion_unavailable"
    assert response.json()["retryable"] is False
    assert "deepseek-flash" not in response.text
    assert harness.wire.gate_receipts() == []


def test_a_non_integer_agent_id_is_rejected(harness: Harness) -> None:
    assert post(harness, "abc").status_code == 422


def test_the_route_does_not_swallow_an_unknown_agent(harness: Harness) -> None:
    response = post(harness, 404404)

    assert response.status_code == 404
    assert response.json()["reason"] == "agent_not_found"


def test_a_missing_draft_key_is_no_suggestion_not_a_server_error(
    harness: Harness, loguru_records: LogRecords
) -> None:
    assert isinstance(harness.catalog, ModelCatalog)
    drafting = harness.catalog.bindings["deepseek-"]

    def build_without_key(context: BuildContext) -> object:
        return context.keys.require("REPLY_SUGGESTION_TEST_UNSET_KEY")

    unkeyed = replace(
        harness.catalog,
        bindings=MappingProxyType(
            {**harness.catalog.bindings, "deepseek-": replace(drafting, build=build_without_key)}
        ),
    )

    path = f"/api/agents/{harness.agent_id}/reply-suggestion"
    with gateway_test_client(catalog=unkeyed) as client:
        responses = [client.post(path), client.post(path)]

    for response in responses:
        assert response.status_code == 200
        assert response.json() == {
            "suggestions": [],
            "reason": "generation_failed",
            "offer_id": None,
        }
    warnings = loguru_records.extras(event="reply_suggestion_model_key_missing")
    assert [extra["stage"] for extra in warnings] == ["draft"]


def test_an_unavailable_memory_indexer_degrades_the_recall_not_the_request(
    harness: Harness, loguru_records: LogRecords
) -> None:
    response = post(harness)

    assert response.status_code == 200
    assert response.json()["suggestions"]
    assert "Reply suggestion memory recall unavailable" in loguru_records.messages("WARNING")
