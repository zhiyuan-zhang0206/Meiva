# meiva:note
# GET /api/events/counts — events counted per value of one dimension, against rows in the real
# stores. The window, filters and 422 mapping are `GET /api/events`' (the same plan); this file
# locks what the counts add: the merge of the two stores into one count per key, the order and the
# `limit` cut with its totals, the filters narrowing the groups, the dimension values, and the
# statement timeout.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import psycopg
import pytest

from base.config.tests.configuration import snapshot_process_config
from gateway.http.config import GatewayConfig
from gateway.tests.auth.gateway_config import gateway_config_of, gateway_test_client
from gateway.tests.events.event_rows import audit_event, telemetry_event


def _get(**params: Any) -> dict[str, Any]:
    with gateway_test_client() as client:
        response = client.get("/api/events/counts", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _groups(body: dict[str, Any]) -> list[tuple[str | None, int]]:
    return [(group["key"], group["count"]) for group in body["groups"]]


def test_an_empty_window_has_no_groups(db_conn: psycopg.Connection) -> None:
    body = _get(by="event_name")
    assert body["groups"] == []
    assert body["meta"]["total"] == 0
    assert body["meta"]["truncated"] is False
    assert body["meta"]["by"] == "event_name"
    assert body["meta"]["window_from"] is not None


def test_a_name_held_by_both_stores_is_one_summed_group(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "send_message", minutes_ago=5)
    audit_event(db_conn, "send_message", minutes_ago=6)
    telemetry_event(db_conn, "llm_usage", minutes_ago=3)
    telemetry_event(db_conn, "send_message", minutes_ago=4, category="log")

    body = _get(by="event_name")

    assert _groups(body) == [("send_message", 3), ("llm_usage", 1)]
    assert body["meta"]["total"] == 4


def test_groups_are_largest_first_with_key_order_on_ties(db_conn: psycopg.Connection) -> None:
    for name in ("b_event", "a_event"):
        telemetry_event(db_conn, name, minutes_ago=5)
    for _ in range(3):
        telemetry_event(db_conn, "turn_end", minutes_ago=5)

    assert _groups(_get(by="event_name")) == [("turn_end", 3), ("a_event", 1), ("b_event", 1)]


def test_limit_cuts_the_groups_but_not_the_total(db_conn: psycopg.Connection) -> None:
    for name, times in (("x", 3), ("y", 2), ("z", 1)):
        for _ in range(times):
            telemetry_event(db_conn, name, minutes_ago=5)

    body = _get(by="event_name", limit=2)

    assert _groups(body) == [("x", 3), ("y", 2)]
    assert body["meta"]["total"] == 6
    assert body["meta"]["truncated"] is True
    assert body["meta"]["limit"] == 2


def test_filters_and_the_window_narrow_what_is_counted(db_conn: psycopg.Connection) -> None:
    telemetry_event(db_conn, "turn_end", minutes_ago=5, machine="m1", agent_id=7)
    telemetry_event(db_conn, "turn_end", minutes_ago=5, machine="m2", agent_id=8)
    telemetry_event(db_conn, "turn_end", minutes_ago=90, machine="m1", agent_id=7)

    assert _groups(_get(by="machine", hours=1)) == [("m1", 1), ("m2", 1)]
    assert _groups(_get(by="machine", agent_id=7)) == [("m1", 2)]
    assert _groups(_get(by="machine", agent_id=7, hours=1)) == [("m1", 1)]


def test_level_agent_and_category_dimensions(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "spawn", minutes_ago=5, agent_id=7, level="warning")
    telemetry_event(db_conn, "turn_end", minutes_ago=5, agent_id=None)
    telemetry_event(db_conn, "x", minutes_ago=5, agent_id=7, category="log", level="error")

    assert _groups(_get(by="level")) == [("error", 1), ("info", 1), ("warning", 1)]
    assert _groups(_get(by="agent_id")) == [("7", 2), (None, 1)]
    assert _groups(_get(by="category")) == [("audit", 1), ("log", 1), ("telemetry", 1)]


def test_a_category_reads_only_its_store(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "spawn", minutes_ago=5)
    telemetry_event(db_conn, "turn_end", minutes_ago=5)

    assert _groups(_get(by="event_name", category="audit")) == [("spawn", 1)]
    assert _groups(_get(by="event_name", category="telemetry")) == [("turn_end", 1)]


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
    with gateway_test_client() as client:
        assert client.get("/api/events/counts", params=params).status_code == 422


# meiva:note
# Hold the telemetry store exclusively, so a read of it waits for the lock.
@contextmanager
def _telemetry_store_locked(db_url: str) -> Generator[None]:
    with psycopg.connect(db_url) as holder:
        holder.execute("LOCK TABLE telemetry_events IN ACCESS EXCLUSIVE MODE")
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
    with _telemetry_store_locked(db_url), gateway_test_client(config=config) as client:
        response = client.get("/api/events/counts", params={"by": "level", "category": "telemetry"})
    assert response.status_code == 503
    assert response.json()["detail"] == "events read timed out"
