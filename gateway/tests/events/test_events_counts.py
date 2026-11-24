from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import psycopg
import pytest

from base.config.tests.configuration import snapshot_process_config
from gateway.events.loki_query import LokiFilters, count_query
from gateway.http.config import GatewayConfig
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_config_of,
    gateway_test_client,
)
from gateway.tests.events.event_rows import audit_event
from gateway.tests.events.loki_stub import LokiStub


@contextmanager
def _loki(
    counts: list[tuple[dict[str, str], int]] | None = None,
) -> Generator[tuple[LokiStub, GatewayTestClient]]:
    with (
        LokiStub() as stub,
        gateway_test_client(fields={"telemetry_loki_url": stub.url}) as client,
    ):
        stub.script.counts = counts or []
        yield stub, client


def _get(loki: tuple[LokiStub, GatewayTestClient], **params: Any) -> dict[str, Any]:
    response = loki[1].get("/api/events/counts", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _groups(body: dict[str, Any]) -> list[tuple[str | None, int]]:
    return [(group["key"], group["count"]) for group in body["groups"]]


def test_an_empty_window_has_no_groups(db_conn: psycopg.Connection) -> None:
    with _loki() as loki:
        body = _get(loki, by="event_name")
    assert body["groups"] == []
    assert body["meta"]["total"] == 0
    assert body["meta"]["truncated"] is False
    assert body["meta"]["by"] == "event_name"
    assert body["meta"]["window_from"] is not None


def test_a_name_held_by_both_stores_is_one_summed_group(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "send_message", minutes_ago=5)
    audit_event(db_conn, "send_message", minutes_ago=6)
    counts = [({"event_name": "llm_usage"}, 1), ({"event_name": "send_message"}, 1)]

    with _loki(counts) as loki:
        body = _get(loki, by="event_name")

    assert _groups(body) == [("send_message", 3), ("llm_usage", 1)]
    assert body["meta"]["total"] == 4


def test_groups_are_largest_first_with_key_order_on_ties(db_conn: psycopg.Connection) -> None:
    counts = [({"event_name": name}, count) for name, count in (("b_event", 1), ("turn_end", 3))]
    counts.append(({"event_name": "a_event"}, 1))

    with _loki(counts) as loki:
        body = _get(loki, by="event_name")

    assert _groups(body) == [("turn_end", 3), ("a_event", 1), ("b_event", 1)]


def test_limit_cuts_the_groups_but_not_the_total(db_conn: psycopg.Connection) -> None:
    counts = [({"event_name": name}, count) for name, count in (("x", 3), ("y", 2), ("z", 1))]

    with _loki(counts) as loki:
        body = _get(loki, by="event_name", limit=2)

    assert _groups(body) == [("x", 3), ("y", 2)]
    assert body["meta"]["total"] == 6
    assert body["meta"]["truncated"] is True
    assert body["meta"]["limit"] == 2


def test_the_filters_and_the_window_are_sent_to_both_stores(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "spawn", minutes_ago=5, machine="m1", agent_id=7)
    audit_event(db_conn, "spawn", minutes_ago=5, machine="m2", agent_id=8)
    audit_event(db_conn, "spawn", minutes_ago=90, machine="m1", agent_id=7)

    with _loki([({"event_machine": "m1"}, 4)]) as loki:
        body = _get(loki, by="machine", agent_id=7, hours=1)
        (query,) = [q for q in loki[0].script.queries if q["path"].endswith("/query")]

    assert _groups(body) == [("m1", 5)]
    expected = count_query(
        LokiFilters(agent_id=7, categories=("telemetry", "log")), by="machine", range_ms=3_600_000
    )
    assert query["query"] == expected


def test_level_agent_and_category_dimensions(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "spawn", minutes_ago=5, agent_id=7, level="warning")

    with _loki([({"level": "error"}, 1), ({"level": "info"}, 1)]) as loki:
        assert _groups(_get(loki, by="level")) == [("error", 1), ("info", 1), ("warning", 1)]
    with _loki([({"agent_id": ""}, 1), ({"agent_id": "7"}, 1)]) as loki:
        assert _groups(_get(loki, by="agent_id")) == [("7", 2), (None, 1)]
    with _loki([({"category": "log"}, 1), ({"category": "telemetry"}, 1)]) as loki:
        assert _groups(_get(loki, by="category")) == [("audit", 1), ("log", 1), ("telemetry", 1)]


def test_a_category_reads_only_its_store(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "spawn", minutes_ago=5)

    with _loki([({"event_name": "turn_end"}, 1)]) as loki:
        assert _groups(_get(loki, by="event_name", category="audit")) == [("spawn", 1)]
        assert loki[0].script.queries == []
        assert _groups(_get(loki, by="event_name", category="telemetry")) == [("turn_end", 1)]


@pytest.mark.parametrize(
    "params",
    [
        {"by": "process"},
        {},
        {"by": "level", "level": "loud"},
        {"by": "level", "category": "metric"},
        {"by": "level", "from": "2026-01-01T00:00:00"},
        {"by": "level", "hours": 1, "from": "2026-01-01T00:00:00Z"},
        {"by": "level", "limit": 0},
        {"by": "level", "limit": 1001},
    ],
)
def test_a_bad_request_is_422(params: dict[str, Any]) -> None:
    with _loki() as loki:
        assert loki[1].get("/api/events/counts", params=params).status_code == 422


def test_a_loki_that_cannot_answer_is_a_retriable_503() -> None:
    with _loki() as loki:
        loki[0].script.status = 500
        response = loki[1].get("/api/events/counts", params={"by": "level"})
    assert response.status_code == 503
    assert response.json()["detail"].startswith("loki backend unavailable")


# meiva:note
# Hold the audit store exclusively, so a read of it waits for the lock.
@contextmanager
def _audit_store_locked(db_url: str) -> Generator[None]:
    with psycopg.connect(db_url) as holder:
        holder.execute("LOCK TABLE audit_events IN ACCESS EXCLUSIVE MODE")
        try:
            yield
        finally:
            holder.rollback()


def test_a_read_that_exceeds_its_statement_timeout_returns_503(db_url: str) -> None:
    config: GatewayConfig = replace(
        gateway_config_of(snapshot_process_config()),
        human_auth="required",
        events_read_timeout_ms=150,
    )
    with _audit_store_locked(db_url), gateway_test_client(config=config) as client:
        response = client.get("/api/events/counts", params={"by": "level", "category": "audit"})
    assert response.status_code == 503
    assert response.json()["detail"] == "events read timed out"
