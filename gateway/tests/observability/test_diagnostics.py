from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from base.paths import meiva_home
from base.telemetry.observability import cluster_label
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.tests.auth.gateway_config import (
    GatewayTestClient,
    gateway_test_client,
    gateway_test_config,
)

_CURRENT_SHA = LOADED_IMAGE.sha or ""
_OLD_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def client_config() -> dict[str, Any]:
    """`GatewayConfig` fields the test client is built with; a test overrides them."""
    return {}


@pytest.fixture
def client(db_conn: psycopg.Connection, client_config: dict[str, Any]) -> Any:
    assert _CURRENT_SHA, "LOADED_IMAGE.sha must be resolved in git worktree"
    db_conn.commit()
    with gateway_test_client(config=gateway_test_config(**client_config)) as test_client:
        yield test_client


def _event(
    db: psycopg.Connection,
    event_name: str,
    *,
    level: str = "error",
    source: str = "svc",
    process: str = "gateway",
    category: str = "telemetry",
    minutes_ago: float = 1,
    cluster: str | None = None,
    deployed_sha: str = _CURRENT_SHA,
    attributes: dict[str, Any] | None = None,
) -> None:
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, machine, cluster, process, category, "
        "event_name, level, source, attributes, deployed_sha) "
        "VALUES (%s, %s, 'm1', %s, %s, %s, %s, %s, %s, %s::jsonb, %s)",
        (
            uuid.uuid4().int % (1 << 62),
            datetime.now(UTC) - timedelta(minutes=minutes_ago),
            cluster_label(meiva_home()) if cluster is None else cluster,
            process,
            category,
            event_name,
            level,
            source,
            json.dumps(attributes or {}),
            deployed_sha,
        ),
    )


def test_strict_admission_excludes_bare_log_warnings_and_noise_events(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    # meiva:note
    # Admitted: error/critical (including bare `log`) and declared `tier="anomaly"` warnings.
    # Rejected: bare `log` warnings, `tier="noise"` events, unregistered warnings, and info rows.
    _event(db_conn, "delivery_stalled", level="warning", minutes_ago=5)
    _event(db_conn, "log", level="error", category="log", minutes_ago=4)
    _event(db_conn, "host_turn_crashed", level="critical", minutes_ago=3)
    _event(db_conn, "log", level="warning", category="log", minutes_ago=2)
    _event(db_conn, "auth401_rejected", level="warning", minutes_ago=2)
    _event(db_conn, "auth401_rejected", level="error", minutes_ago=2)
    _event(db_conn, "unregistered_warning", level="warning", minutes_ago=2)
    _event(db_conn, "turn_end", level="warning", minutes_ago=2)
    _event(db_conn, "delivery_stalled", level="info", minutes_ago=2)
    db_conn.commit()

    body = client.get("/api/stats/diagnostics").json()
    assert body["deployed_sha"] == _CURRENT_SHA
    names = {(r["event_name"], r["level"]) for r in body["classes"]}
    assert names == {
        ("delivery_stalled", "warning"),
        ("log", "error"),
        ("host_turn_crashed", "critical"),
    }


def test_prior_release_errors_auto_archive_and_regressions_reopen_on_current_sha(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    _event(db_conn, "old_only", level="error", deployed_sha=_OLD_SHA, minutes_ago=5)
    _event(db_conn, "pre_column", level="error", deployed_sha="", minutes_ago=5)
    _event(db_conn, "regressed_err", level="error", deployed_sha=_OLD_SHA, minutes_ago=20)
    _event(db_conn, "regressed_err", level="error", deployed_sha=_CURRENT_SHA, minutes_ago=2)
    _event(db_conn, "fresh_err", level="error", deployed_sha=_CURRENT_SHA, minutes_ago=1)
    db_conn.commit()

    active = client.get("/api/stats/diagnostics").json()
    assert {
        (r["event_name"], r["regressed"], r["current_sha_count"]) for r in active["classes"]
    } == {
        ("regressed_err", True, 1),
        ("fresh_err", False, 1),
    }

    history = client.get("/api/stats/diagnostics", params={"include_archived": True}).json()
    assert {r["event_name"]: r["archived_by"] for r in history["classes"]} == {
        "old_only": "deployed_sha",
        "pre_column": "deployed_sha",
        "regressed_err": None,
        "fresh_err": None,
    }


def test_quiet_window_auto_archives_transient_current_sha_errors_and_reopens_on_recurrence(
    db_conn: psycopg.Connection, client: TestClient
) -> None:
    _event(db_conn, "transient_err", level="error", minutes_ago=180)
    _event(db_conn, "live_err", level="error", minutes_ago=2)
    db_conn.commit()

    active = client.get("/api/stats/diagnostics").json()
    assert [(r["event_name"], r["archived_by"]) for r in active["classes"]] == [("live_err", None)]

    history = client.get("/api/stats/diagnostics", params={"include_archived": True}).json()
    assert {r["event_name"]: r["archived_by"] for r in history["classes"]} == {
        "transient_err": "quiet",
        "live_err": None,
    }

    _event(db_conn, "transient_err", level="error", minutes_ago=0)
    db_conn.commit()
    reopened = client.get("/api/stats/diagnostics").json()
    assert {r["event_name"] for r in reopened["classes"]} == {"transient_err", "live_err"}


@pytest.mark.parametrize("client_config", [{"diagnostic_quiet_seconds": 60.0}])
def test_the_quiet_window_is_the_configured_one(
    db_conn: psycopg.Connection, client: GatewayTestClient
) -> None:
    _event(db_conn, "three_minutes_ago", level="error", minutes_ago=3)
    db_conn.commit()

    assert client.get("/api/stats/diagnostics").json()["classes"] == []
    dashboard = client.get("/api/stats/dashboard").json()
    assert dashboard["diagnostics"]["active_classes"] == 0
    assert dashboard["diagnostics"]["archived_classes"] == 1


@pytest.mark.parametrize("client_config", [{"diagnostics_limit": 2}])
def test_the_class_list_is_capped_but_reports_its_full_size(
    db_conn: psycopg.Connection, client: GatewayTestClient
) -> None:
    for name, repeats in (("a", 3), ("b", 2), ("c", 1)):
        for _ in range(repeats):
            _event(db_conn, name, level="error")
    db_conn.commit()

    body = client.get("/api/stats/diagnostics").json()
    assert [r["event_name"] for r in body["classes"]] == ["a", "b"]
    assert (body["total_classes"], body["total_events"]) == (3, 6)


@pytest.mark.parametrize("client_config", [{"diagnostic_samples_limit": 2}])
def test_samples_are_the_newest_events_of_one_class_with_deployed_sha(
    db_conn: psycopg.Connection, client: GatewayTestClient
) -> None:
    _event(db_conn, "boom", level="error", minutes_ago=30, attributes={"msg": "oldest"})
    _event(
        db_conn,
        "boom",
        level="error",
        minutes_ago=20,
        deployed_sha=_OLD_SHA,
        attributes={"msg": "middle", "detail": 7},
    )
    _event(db_conn, "boom", level="error", minutes_ago=10, attributes={"msg": "newest"})
    _event(db_conn, "boom", level="error", process="agent-host", attributes={"msg": "other"})
    db_conn.commit()

    samples = client.get(
        "/api/stats/diagnostics/samples",
        params={"level": "error", "event_name": "boom", "source": "svc", "process": "gateway"},
    ).json()["samples"]

    assert [s["message"] for s in samples] == ["newest", "middle"]
    assert [s["deployed_sha"] for s in samples] == [_CURRENT_SHA, _OLD_SHA]
    assert samples[1]["attributes"] == {"msg": "middle", "detail": 7}


def test_retired_event_resolutions_and_alert_classes_routes_are_removed(
    client: TestClient,
) -> None:
    assert client.get("/api/event-resolutions").status_code == 404
    assert client.post("/api/event-resolutions", json={}).status_code == 404
    assert client.get("/api/stats/alert-classes").status_code == 404
