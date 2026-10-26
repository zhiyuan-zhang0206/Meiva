# meiva:note
# `GET /api/agents/{agent_id}/events` — historical REST slice over `telemetry_events`.
#
# The request-to-read mapping (agent scope, telemetry/log-only categories, exact level, window,
# paging) is asserted over plain values in `gateway/events/tests/test_events_plan.py`. These tests
# read rows back through the route: scoping, ordering, filters, the wire shape, the configured
# default page and the 503 on a read past its statement timeout.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import psycopg

from base.config.tests.configuration import snapshot_process_config
from gateway.http.config import GatewayConfig
from gateway.tests.auth.gateway_config import gateway_config_of, gateway_test_client
from gateway.tests.events.event_rows import audit_event, telemetry_event


def _get(params: dict[str, Any] | None = None, *, agent_id: int = 7) -> list[dict[str, Any]]:
    with gateway_test_client() as client:
        response = client.get(f"/api/agents/{agent_id}/events", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _events(items: list[dict[str, Any]]) -> list[str]:
    return [item["event"] for item in items]


class TestAgentEventsQuery:
    def test_returns_rows_newest_first(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=30, attributes={"msg": "oldest"})
        telemetry_event(db_conn, "turn_end", minutes_ago=1, attributes={"msg": "newest"})
        items = _get()
        assert [i["payload"]["msg"] for i in items] == ["newest", "oldest"]
        # meiva:note
        # wire shape includes the canonical-line identity shared by all event APIs
        assert set(items[0]) == {"id", "line_sha256", "ts", "agent_id", "level", "event", "payload"}

    def test_scopes_to_agent(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=1, agent_id=42)
        telemetry_event(db_conn, "turn_end", minutes_ago=2, agent_id=7)
        telemetry_event(db_conn, "node_exit", minutes_ago=3, agent_id=None)
        assert _events(_get(agent_id=42)) == ["llm_usage"]

    # meiva:note
    # Audit rows (spawn/send_message/...) must not leak into the feed.
    def test_telemetry_and_log_categories_only(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=1)
        telemetry_event(db_conn, "log_line", minutes_ago=2, category="log")
        audit_event(db_conn, "spawn", minutes_ago=3)
        assert _events(_get()) == ["llm_usage", "log_line"]

    def test_filter_event_is_exact(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=1)
        telemetry_event(db_conn, "turn_end", minutes_ago=2)
        assert _events(_get({"event": "llm_usage"})) == ["llm_usage"]

    def test_filter_level_is_exact_and_case_insensitive(self, db_conn: psycopg.Connection) -> None:
        # meiva:note
        # exact level (case-insensitive) — NOT a minimum threshold
        telemetry_event(db_conn, "llm_usage", minutes_ago=1, level="warning")
        telemetry_event(db_conn, "turn_end", minutes_ago=2, level="error")
        telemetry_event(db_conn, "node_exit", minutes_ago=3, level="info")
        assert _events(_get({"level": "WARNING"})) == ["llm_usage"]

    def test_from_to_window(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=10)
        assert _get({"from": "2020-01-01T00:00:00Z", "to": "2020-01-02T00:00:00Z"}) == []
        assert _events(_get({"from": "2020-01-01T00:00:00Z"})) == ["llm_usage"]

    def test_no_from_means_the_last_day(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=60)
        telemetry_event(db_conn, "turn_end", minutes_ago=60 * 30)
        assert _events(_get()) == ["llm_usage"]

    def test_limit_and_offset_page_through_the_rows(self, db_conn: psycopg.Connection) -> None:
        for minutes, name in enumerate(["a_event", "b_event", "c_event", "d_event"], start=1):
            telemetry_event(db_conn, name, minutes_ago=minutes)
        assert _events(_get({"limit": 2, "offset": 1})) == ["b_event", "c_event"]

    # meiva:note
    # The implicit page is the ``display.events_default_limit`` field
    # (``MEIVA_EVENTS_DEFAULT_LIMIT``); the literal 100 is only that field's
    # default, not a hard-coded page size.
    def test_default_limit_comes_from_display_config(self, db_conn: psycopg.Connection) -> None:
        for minutes in range(3):
            telemetry_event(db_conn, "llm_usage", minutes_ago=minutes + 1)
        with gateway_test_client(fields={"events_default_limit": 2}) as client:
            response = client.get("/api/agents/7/events")
        assert len(response.json()) == 2

    def test_unknown_agent_returns_empty(self) -> None:
        assert _get(agent_id=999999) == []

    def test_an_illegal_request_is_422(self) -> None:
        with gateway_test_client() as client:
            for params in ({"offset": 10_001}, {"limit": 1001}, {"from": "not-a-date"}):
                assert client.get("/api/agents/7/events", params=params).status_code == 422


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


def test_a_read_that_exceeds_its_statement_timeout_is_503(db_url: str) -> None:
    config: GatewayConfig = replace(
        gateway_config_of(snapshot_process_config()),
        human_auth="required",
        events_read_timeout_ms=150,
    )
    with _telemetry_store_locked(db_url), gateway_test_client(config=config) as client:
        response = client.get("/api/agents/7/events")
    assert response.status_code == 503
    assert response.json()["detail"] == "events read timed out"
