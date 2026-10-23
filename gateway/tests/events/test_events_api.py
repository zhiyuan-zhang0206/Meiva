# meiva:note
# GET /api/events — unified event stream query, against rows in the real stores.
#
# The request-to-read decisions (filters, window, stores, page depth) are asserted over plain
# values in `gateway/events/tests/test_events_plan.py`; the two-store merge is in
# `test_events_api_audit.py`. This file locks what the endpoint adds around them: the unified row
# shape and tiers, newest-first order, the opt-in exact total, the lookahead `has_more`, paging, the
# configured default window, the 422 mapping and the 503 on a read past its statement timeout.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from typing import Any

import psycopg
import pytest

from base.config.tests.configuration import snapshot_process_config
from gateway.http.config import GatewayConfig, gateway_config_from_boot
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.event_rows import audit_event, telemetry_event

_EVENT_KEYS = {
    "id",
    "line_sha256",
    "ts",
    "trace_id",
    "span_id",
    "agent_id",
    "machine",
    "process",
    "category",
    "event_name",
    "tier",
    "level",
    "source",
    "target_agent_id",
    "attributes",
}


def _get(**params: Any) -> dict[str, Any]:
    with gateway_test_client() as client:
        response = client.get("/api/events", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _names(body: dict[str, Any]) -> list[str]:
    return [item["event_name"] for item in body["items"]]


class TestEventsApi:
    def test_empty_returns_empty_envelope(self, db_conn: psycopg.Connection) -> None:
        body = _get()
        assert body["items"] == []
        assert body["meta"]["total"] is None  # exact count is opt-in
        assert body["meta"]["has_more"] is False
        assert body["meta"]["limit"] == 100
        assert body["meta"]["offset"] == 0
        assert body["meta"]["window_from"] is not None  # 24h default echoes

    def test_returns_newest_first_with_unified_shape(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=30, attributes={"msg": "old"})
        telemetry_event(db_conn, "turn_end", minutes_ago=5, attributes={"msg": "new"})
        items = _get()["items"]
        assert [item["event_name"] for item in items] == ["turn_end", "llm_usage"]
        assert set(items[0]) == _EVENT_KEYS
        assert items[0]["attributes"] == {"msg": "new"}

    def test_response_rows_include_computed_tiers(self, db_conn: psycopg.Connection) -> None:
        audit_event(db_conn, "spawn", minutes_ago=1)
        telemetry_event(db_conn, "llm_usage", minutes_ago=2)
        telemetry_event(db_conn, "node_exit", minutes_ago=3)
        telemetry_event(db_conn, "llm_usage", minutes_ago=4, level="warning")
        assert [item["tier"] for item in _get()["items"]] == [
            "business",
            "observation",
            "noise",
            "anomaly",
        ]

    def test_filters_compose_and(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=1, agent_id=7, machine="machine-1")
        telemetry_event(db_conn, "llm_usage", minutes_ago=2, agent_id=8, machine="machine-1")
        telemetry_event(db_conn, "llm_usage", minutes_ago=3, agent_id=7, machine="machine-2")
        telemetry_event(db_conn, "turn_end", minutes_ago=4, agent_id=7, machine="machine-1")
        telemetry_event(
            db_conn, "llm_usage", minutes_ago=5, agent_id=7, machine="machine-1", level="warning"
        )
        body = _get(
            category="telemetry",
            event_name="llm_usage",
            agent_id=7,
            machine="machine-1",
            level="INFO",
        )
        assert _names(body) == ["llm_usage"]
        assert [item["agent_id"] for item in body["items"]] == [7]
        assert [item["level"] for item in body["items"]] == ["info"]

    def test_tier_filter_maps_a_comma_separated_list(self, db_conn: psycopg.Connection) -> None:
        audit_event(db_conn, "spawn", minutes_ago=1)
        telemetry_event(db_conn, "llm_usage", minutes_ago=2)
        telemetry_event(db_conn, "llm_usage", minutes_ago=3, level="warning")
        assert _names(_get(tier="business,anomaly")) == ["spawn", "llm_usage"]

    def test_kind_param_is_ignored_not_filtered(self, db_conn: psycopg.Connection) -> None:
        """Legacy `kind=` alias removed (2026-09-21): an undeclared query
        param is ignored by FastAPI, so the request runs unfiltered — pinned
        deliberately as the post-removal wire semantics."""
        telemetry_event(db_conn, "turn_end", minutes_ago=1)
        assert _names(_get(kind="llm_usage")) == ["turn_end"]

    def test_window_by_hours_and_default_day(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=30)
        telemetry_event(db_conn, "turn_end", minutes_ago=90)
        telemetry_event(db_conn, "node_exit", minutes_ago=60 * 30)
        assert _names(_get(hours=1)) == ["llm_usage"]
        assert _names(_get()) == ["llm_usage", "turn_end"]

    def test_explicit_window(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=30)
        body = _get(**{"from": "2020-01-01T00:00:00Z", "to": "2020-01-02T00:00:00Z"})
        assert body["items"] == []
        assert body["meta"]["window_from"].startswith("2020-01-01T00:00:00")
        assert body["meta"]["window_to"].startswith("2020-01-02T00:00:00")

    def test_total_skipped_by_default(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=1)
        assert _get()["meta"]["total"] is None

    def test_meta_total_opt_in_counts_the_whole_filtered_window(
        self, db_conn: psycopg.Connection
    ) -> None:
        for minutes in range(3):
            telemetry_event(db_conn, "llm_usage", minutes_ago=minutes + 1)
        body = _get(limit=1, with_total=1)
        assert body["meta"]["total"] == 3
        assert len(body["items"]) == 1
        # meiva:note
        # the fetch's +1 lookahead
        assert body["meta"]["has_more"] is True

    def test_has_more_comes_from_the_lookahead(self, db_conn: psycopg.Connection) -> None:
        telemetry_event(db_conn, "llm_usage", minutes_ago=1)
        telemetry_event(db_conn, "turn_end", minutes_ago=2)
        assert _get(limit=1)["meta"]["has_more"] is True
        assert _get(limit=2)["meta"]["has_more"] is False

    def test_limit_offset_paging(self, db_conn: psycopg.Connection) -> None:
        for minutes, name in enumerate(["a_event", "b_event", "c_event", "d_event"], start=1):
            telemetry_event(db_conn, name, minutes_ago=minutes)
        page = _get(category="telemetry", limit=2, offset=1)
        assert _names(page) == ["b_event", "c_event"]
        assert (page["meta"]["limit"], page["meta"]["offset"]) == (2, 1)

    # meiva:note
    # The implicit window is the ``display.events_default_limit`` field
    # (``MEIVA_EVENTS_DEFAULT_LIMIT``); the literal 100 is only that field's
    # default, not a hard-coded page size. The meta echo reports the
    # resolved window, not the raw None.
    def test_default_limit_comes_from_display_config(self, db_conn: psycopg.Connection) -> None:
        for minutes in range(3):
            telemetry_event(db_conn, "llm_usage", minutes_ago=minutes + 1)
        with gateway_test_client(fields={"events_default_limit": 2}) as client:
            response = client.get("/api/events")
        assert response.status_code == 200
        assert len(response.json()["items"]) == 2
        assert response.json()["meta"]["limit"] == 2

    def test_no_match_returns_empty(self, db_conn: psycopg.Connection) -> None:
        body = _get(event_name="does_not_exist")
        assert body["items"] == []
        assert body["meta"]["total"] is None

    @pytest.mark.parametrize(
        "params",
        [
            {"category": "bogus"},
            {"level": "fatal"},
            {"tier": "business,urgent"},
            {"from": "not-a-date"},
            {"from": "2026-08-04T00:00:00"},
            {"to": "2026-08-04T00:00:00"},
            {"from": "2026-08-04T00:00:00Z", "hours": 1},
            {"limit": 1001},
            {"offset": 10_001},
        ],
    )
    def test_an_illegal_request_is_422(self, params: dict[str, Any]) -> None:
        with gateway_test_client() as client:
            assert client.get("/api/events", params=params).status_code == 422


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
        gateway_config_from_boot(snapshot_process_config()),
        human_auth="required",
        events_read_timeout_ms=150,
    )
    with _telemetry_store_locked(db_url), gateway_test_client(config=config) as client:
        response = client.get("/api/events", params={"category": "telemetry"})
    assert response.status_code == 503
    assert response.json()["detail"] == "events read timed out"
