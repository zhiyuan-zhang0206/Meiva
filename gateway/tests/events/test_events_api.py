from __future__ import annotations

import hashlib
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from base.config.tests.configuration import snapshot_process_config
from base.telemetry import event_id
from gateway.events.loki_query import LokiFilters, log_query
from gateway.http.config import GatewayConfig
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_config_of,
    gateway_test_client,
)
from gateway.tests.events.event_rows import audit_event
from gateway.tests.events.loki_stub import LokiStub, event_line

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


@contextmanager
def _loki(**fields: Any) -> Generator[tuple[LokiStub, GatewayTestClient]]:
    with (
        LokiStub() as stub,
        gateway_test_client(fields={"telemetry_loki_url": stub.url, **fields}) as client,
    ):
        yield stub, client


def _store(stub: LokiStub, name: str, *, minutes_ago: float, **line: Any) -> None:
    ts = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    stub.script.lines.append(event_line(ts, name, **line))


def _get(stub_client: tuple[LokiStub, GatewayTestClient], **params: Any) -> dict[str, Any]:
    response = stub_client[1].get("/api/events", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _names(body: dict[str, Any]) -> list[str]:
    return [item["event_name"] for item in body["items"]]


def _log_queries(stub: LokiStub) -> list[str]:
    return [q["query"] for q in stub.script.queries if q["path"].endswith("query_range")]


class TestEventsApi:
    def test_empty_returns_empty_envelope(self, db_conn: psycopg.Connection) -> None:
        with _loki() as loki:
            body = _get(loki)
        assert body["items"] == []
        assert body["meta"]["total"] is None
        assert body["meta"]["has_more"] is False
        assert body["meta"]["next_to"] is None
        assert body["meta"]["limit"] == 100
        assert "offset" not in body["meta"]
        assert body["meta"]["window_from"] is not None

    def test_returns_newest_first_with_unified_shape(self, db_conn: psycopg.Connection) -> None:
        with _loki() as loki:
            _store(loki[0], "llm_usage", minutes_ago=30, attributes={"msg": "old"})
            _store(loki[0], "turn_end", minutes_ago=5, attributes={"msg": "new"})
            items = _get(loki)["items"]
        assert [item["event_name"] for item in items] == ["turn_end", "llm_usage"]
        assert set(items[0]) == _EVENT_KEYS
        assert items[0]["attributes"] == {"msg": "new"}
        assert items[0]["category"] == "telemetry"

    def test_a_row_has_the_identity_the_emitter_gave_it(self, db_conn: psycopg.Connection) -> None:
        with _loki() as loki:
            ts = datetime.now(UTC) - timedelta(minutes=1)
            stamp, line = event_line(ts, "turn_end")
            loki[0].script.lines.append((stamp, line))
            (item,) = _get(loki)["items"]
        assert item["id"] == event_id(line, stamp)
        assert item["line_sha256"] == hashlib.sha256(line.encode()).hexdigest()

    def test_response_rows_include_computed_tiers(self, db_conn: psycopg.Connection) -> None:
        audit_event(db_conn, "spawn", minutes_ago=1)
        with _loki() as loki:
            _store(loki[0], "llm_usage", minutes_ago=2)
            _store(loki[0], "node_exit", minutes_ago=3)
            _store(loki[0], "llm_usage", minutes_ago=4, level="warning")
            body = _get(loki)
        assert [item["tier"] for item in body["items"]] == [
            "business",
            "observation",
            "noise",
            "anomaly",
        ]

    def test_filters_reach_loki_as_one_query(self, db_conn: psycopg.Connection) -> None:
        with _loki() as loki:
            _get(
                loki,
                category="telemetry",
                event_name="llm_usage",
                agent_id=7,
                machine="machine-1",
                level="INFO",
            )
        assert _log_queries(loki[0]) == [
            log_query(
                LokiFilters(
                    agent_id=7,
                    event_names=("llm_usage",),
                    level="info",
                    categories=("telemetry",),
                    machine="machine-1",
                )
            )
        ]

    def test_tier_filter_reads_both_stores(self, db_conn: psycopg.Connection) -> None:
        audit_event(db_conn, "spawn", minutes_ago=1)
        audit_event(db_conn, "send_message", minutes_ago=2, level="warning")
        with _loki() as loki:
            _store(loki[0], "llm_usage", minutes_ago=3, level="warning")
            body = _get(loki, tier="business,anomaly")
        assert _names(body) == ["spawn", "send_message", "llm_usage"]
        (query,) = _log_queries(loki[0])
        assert query == log_query(
            LokiFilters(categories=("telemetry", "log"), tiers=("business", "anomaly"))
        )

    def test_kind_param_is_ignored_not_filtered(self, db_conn: psycopg.Connection) -> None:
        """Legacy `kind=` alias removed (2026-09-21): an undeclared query
        param is ignored by FastAPI, so the request runs unfiltered — pinned
        deliberately as the post-removal wire semantics."""
        with _loki() as loki:
            _store(loki[0], "turn_end", minutes_ago=1)
            assert _names(_get(loki, kind="llm_usage")) == ["turn_end"]

    def test_window_by_hours_and_default_day(self, db_conn: psycopg.Connection) -> None:
        with _loki() as loki:
            _store(loki[0], "llm_usage", minutes_ago=30)
            _store(loki[0], "turn_end", minutes_ago=90)
            _store(loki[0], "node_exit", minutes_ago=60 * 30)
            assert _names(_get(loki, hours=1)) == ["llm_usage"]
            assert _names(_get(loki)) == ["llm_usage", "turn_end"]

    def test_explicit_window(self, db_conn: psycopg.Connection) -> None:
        with _loki() as loki:
            _store(loki[0], "llm_usage", minutes_ago=30)
            body = _get(loki, **{"from": "2020-01-01T00:00:00Z", "to": "2020-01-02T00:00:00Z"})
        assert body["items"] == []
        assert body["meta"]["window_from"].startswith("2020-01-01T00:00:00")
        assert body["meta"]["window_to"].startswith("2020-01-02T00:00:00")

    def test_rows_outside_the_window_by_a_sub_microsecond_are_dropped(
        self, db_conn: psycopg.Connection
    ) -> None:
        ts = datetime(2026, 11, 20, 12, 0, 0, 500, tzinfo=UTC)
        with _loki() as loki:
            stamp, line = event_line(ts, "turn_end")
            loki[0].script.lines.append((stamp - 300, line))
            window = {"from": "2026-11-20T00:00:00Z", "to": "2026-11-20T12:00:00.000499Z"}
            assert _get(loki, **window)["items"] == []
            inclusive = {**window, "to": "2026-11-20T12:00:00.000500Z"}
            assert _names(_get(loki, **inclusive)) == ["turn_end"]

    def test_total_skipped_by_default(self, db_conn: psycopg.Connection) -> None:
        with _loki() as loki:
            _store(loki[0], "llm_usage", minutes_ago=1)
            assert _get(loki)["meta"]["total"] is None
        assert [q["path"] for q in loki[0].script.queries] == ["/loki/api/v1/query_range"]

    def test_meta_total_opt_in_counts_the_whole_filtered_window(
        self, db_conn: psycopg.Connection
    ) -> None:
        audit_event(db_conn, "spawn", minutes_ago=1)
        with _loki() as loki:
            for minutes in range(3):
                _store(loki[0], "llm_usage", minutes_ago=minutes + 1)
            loki[0].script.counts = [({}, 3)]
            body = _get(loki, limit=1, with_total=1)
        assert body["meta"]["total"] == 4  # three telemetry lines + one audit row
        assert len(body["items"]) == 1
        # meiva:note
        # the fetch's +1 lookahead
        assert body["meta"]["has_more"] is True
        (count,) = [q for q in loki[0].script.queries if q["path"] == "/loki/api/v1/query"]
        assert count["query"].startswith("sum(count_over_time({")

    def test_has_more_comes_from_the_lookahead(self, db_conn: psycopg.Connection) -> None:
        with _loki() as loki:
            _store(loki[0], "llm_usage", minutes_ago=1)
            _store(loki[0], "turn_end", minutes_ago=2)
            assert _get(loki, limit=1)["meta"]["has_more"] is True
            assert _get(loki, limit=2)["meta"]["has_more"] is False

    def test_the_next_page_is_the_window_below_the_oldest_row(
        self, db_conn: psycopg.Connection
    ) -> None:
        with _loki() as loki:
            for minutes, name in enumerate(["a_event", "b_event", "c_event", "d_event"], start=1):
                _store(loki[0], name, minutes_ago=minutes)
            first = _get(loki, category="telemetry", limit=2)
            assert _names(first) == ["a_event", "b_event"]
            assert first["meta"]["has_more"] is True
            oldest = datetime.fromisoformat(first["items"][-1]["ts"])
            assert datetime.fromisoformat(first["meta"]["next_to"]) == oldest - timedelta(
                microseconds=1
            )
            second = _get(loki, category="telemetry", limit=2, to=first["meta"]["next_to"])
            assert _names(second) == ["c_event", "d_event"]
            assert second["meta"]["has_more"] is False
            assert second["meta"]["next_to"] is None

    # meiva:note
    # The implicit window is the ``display.events_default_limit`` field
    # (``MEIVA_EVENTS_DEFAULT_LIMIT``); the literal 100 is only that field's
    # default, not a hard-coded page size. The meta echo reports the
    # resolved window, not the raw None.
    def test_default_limit_comes_from_display_config(self, db_conn: psycopg.Connection) -> None:
        with _loki(events_default_limit=2) as loki:
            for minutes in range(3):
                _store(loki[0], "llm_usage", minutes_ago=minutes + 1)
            body = _get(loki)
        assert len(body["items"]) == 2
        assert body["meta"]["limit"] == 2

    def test_no_match_returns_empty(self, db_conn: psycopg.Connection) -> None:
        with _loki() as loki:
            body = _get(loki, event_name="does_not_exist")
        assert body["items"] == []
        assert body["meta"]["total"] is None

    def test_the_audit_category_does_not_read_loki(self, db_conn: psycopg.Connection) -> None:
        audit_event(db_conn, "spawn", minutes_ago=1)
        with _loki() as loki:
            assert _names(_get(loki, category="audit")) == ["spawn"]
        assert loki[0].script.queries == []

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
        ],
    )
    def test_an_illegal_request_is_422(self, params: dict[str, Any]) -> None:
        with _loki() as loki:
            assert loki[1].get("/api/events", params=params).status_code == 422
        assert loki[0].script.queries == []

    def test_a_loki_that_cannot_answer_is_a_retriable_503(
        self, db_conn: psycopg.Connection
    ) -> None:
        with _loki() as loki:
            loki[0].script.status = 500
            response = loki[1].get("/api/events")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "1"
        assert response.json()["detail"].startswith("loki backend unavailable")

    def test_a_loki_that_is_down_is_a_retriable_503(self, db_conn: psycopg.Connection) -> None:
        with LokiStub() as stub:
            url = stub.url
        with gateway_test_client(fields={"telemetry_loki_url": url}) as client:
            response = client.get("/api/events", params={"category": "telemetry"})
        assert response.status_code == 503


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
        response = client.get("/api/events", params={"category": "audit"})
    assert response.status_code == 503
    assert response.json()["detail"] == "events read timed out"
