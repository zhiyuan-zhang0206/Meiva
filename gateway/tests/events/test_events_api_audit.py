from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.events.event_rows import audit_event
from gateway.tests.events.loki_stub import LokiStub, event_line


@contextmanager
def _stub() -> Generator[LokiStub]:
    with LokiStub() as stub:
        yield stub


def _telemetry(stub: LokiStub, name: str, *, minutes_ago: float) -> None:
    stub.script.lines.append(event_line(datetime.now(UTC) - timedelta(minutes=minutes_ago), name))


def _get(stub: LokiStub, **params: Any) -> dict[str, Any]:
    with gateway_test_client(fields={"telemetry_loki_url": stub.url}) as client:
        response = client.get("/api/events", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_the_audit_category_is_served_from_audit_events_alone(db_conn: psycopg.Connection) -> None:
    older = audit_event(db_conn, "send_message", minutes_ago=30)
    newer = audit_event(db_conn, "spawn", minutes_ago=10)

    with _stub() as stub:
        _telemetry(stub, "llm_usage", minutes_ago=5)
        body = _get(stub, category="audit")

    assert [item["event_name"] for item in body["items"]] == ["spawn", "send_message"]
    assert [item["id"] for item in body["items"]] == [newer, older]
    assert {item["category"] for item in body["items"]} == {"audit"}
    assert {item["tier"] for item in body["items"]} == {"business"}


def test_an_audit_only_event_name_skips_telemetry_and_a_telemetry_name_skipsaudit_event(
    db_conn: psycopg.Connection,
) -> None:
    audit_event(db_conn, "spawn", minutes_ago=5)

    with _stub() as stub:
        _telemetry(stub, "llm_usage", minutes_ago=3)
        spawn = _get(stub, event_name="spawn")
        assert stub.script.queries == []
        usage = _get(stub, event_name="llm_usage")
    assert [item["event_name"] for item in spawn["items"]] == ["spawn"]
    assert [item["event_name"] for item in usage["items"]] == ["llm_usage"]


def test_without_a_category_both_tables_merge_newest_first(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "send_message", minutes_ago=40)
    audit_event(db_conn, "spawn", minutes_ago=20)

    with _stub() as stub:
        _telemetry(stub, "llm_usage", minutes_ago=30)
        _telemetry(stub, "turn_end", minutes_ago=10)
        body = _get(stub)

    assert [item["event_name"] for item in body["items"]] == [
        "turn_end",
        "spawn",
        "llm_usage",
        "send_message",
    ]


def test_a_merged_page_is_cut_after_the_merge_and_the_cursor_walks_both_stores(
    db_conn: psycopg.Connection,
) -> None:
    for minutes in (10, 30, 50):
        audit_event(db_conn, "spawn", minutes_ago=minutes)

    with _stub() as stub:
        _telemetry(stub, "llm_usage", minutes_ago=20)
        _telemetry(stub, "llm_usage", minutes_ago=40)
        first = _get(stub, limit=2)
        second = _get(stub, limit=2, to=first["meta"]["next_to"])
        last = _get(stub, limit=2, to=second["meta"]["next_to"])

    assert [item["event_name"] for item in first["items"]] == ["spawn", "llm_usage"]
    assert first["meta"]["has_more"] is True
    assert [item["event_name"] for item in second["items"]] == ["spawn", "llm_usage"]
    assert second["meta"]["has_more"] is True
    assert [item["event_name"] for item in last["items"]] == ["spawn"]
    assert last["meta"]["has_more"] is False
    assert last["meta"]["next_to"] is None


def test_the_total_sums_both_stores(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "spawn", minutes_ago=5)
    audit_event(db_conn, "send_message", minutes_ago=6)

    with _stub() as stub:
        stub.script.counts = [({}, 3)]
        assert _get(stub, with_total=1)["meta"]["total"] == 5
        assert _get(stub, category="audit", with_total=1)["meta"]["total"] == 2


def test_audit_rows_honour_the_agent_level_machine_trace_and_window_filters(
    db_conn: psycopg.Connection,
) -> None:
    audit_event(db_conn, "spawn", minutes_ago=5, agent_id=1, machine="m1", trace_id="ab12")
    audit_event(db_conn, "spawn", minutes_ago=5, agent_id=2, machine="m2")
    audit_event(db_conn, "env_unauthorized_write", minutes_ago=5, agent_id=1, level="warning")
    audit_event(db_conn, "spawn", minutes_ago=3000, agent_id=1)

    with _stub() as stub:
        assert len(_get(stub, category="audit", agent_id=1)["items"]) == 2
        assert len(_get(stub, category="audit", agent_id=1, hours=100)["items"]) == 3
        assert len(_get(stub, category="audit", machine="m2")["items"]) == 1
        assert len(_get(stub, category="audit", trace_id="AB12")["items"]) == 1
        warnings = _get(stub, category="audit", level="WARNING")["items"]
    assert [i["event_name"] for i in warnings] == ["env_unauthorized_write"]


def test_tiers_select_audit_rows_by_level(db_conn: psycopg.Connection) -> None:
    audit_event(db_conn, "spawn", minutes_ago=5)
    audit_event(db_conn, "env_unauthorized_write", minutes_ago=6, level="warning")

    with _stub() as stub:
        business = _get(stub, category="audit", tier="business")["items"]
        anomaly = _get(stub, category="audit", tier="anomaly")["items"]
        observation = _get(stub, category="audit", tier="observation")["items"]

    assert [item["event_name"] for item in business] == ["spawn"]
    assert [item["event_name"] for item in anomaly] == ["env_unauthorized_write"]
    assert {item["tier"] for item in anomaly} == {"anomaly"}
    assert observation == []
