# meiva:note
# Integration tests for GET /api/stats/dashboard HTTP.
# Locks the query contract of the sidebar Status popover — pyright/tsc cannot catch
# drift between hard-coded `payload->>'X'` keys in SQL and emit site field names,
# these tests are the only defense.
#
# Runs on meiva_test DB (real SQL): the window's telemetry rows are INSERTed into
# `telemetry_events` (audit rows into `audit_events`), and the `agents` table's
# live_count is populated via INSERT of real rows.
# meiva:link gateway/cluster/docs/ops-surfaces.md

from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from base.api_contracts.agent_contract import harness_billing_of_source
from base.paths import meiva_home
from base.telemetry.observability import cluster_label
from gateway.cluster.process_boot import LOADED_IMAGE
from gateway.schemas.stats import StatsWindowHours, window_delta
from gateway.tests.auth.gateway_config import gateway_test_client


def _reported_count(value: object) -> int | None:
    return value if type(value) is int else None


class _EventRows:
    """Writes the events a test names into the tables the dashboard reads."""

    def __init__(self, db: psycopg.Connection) -> None:
        self._db = db

    def add(
        self,
        *,
        event: str,
        agent_id: int | None = None,
        level: str = "info",
        payload: dict[str, Any] | None = None,
        ts_offset_hours: float = 0,
        ts: datetime | None = None,
        category: str = "telemetry",
        cluster: str | None = None,
        deployed_sha: str | None = None,
    ) -> None:
        when = ts if ts is not None else datetime.now(UTC) - timedelta(hours=ts_offset_hours)
        uid = uuid.uuid4().int % (1 << 62)
        attributes = json.dumps(payload or {})
        sha = (LOADED_IMAGE.sha or "") if deployed_sha is None else deployed_sha
        if category == "audit":
            self._db.execute(
                "INSERT INTO audit_events (event_uid, ts, agent_id, machine, process, event_name, "
                "level, source, attributes) VALUES (%s, %s, %s, 'test', 'test', %s, %s, 'test', "
                "%s::jsonb)",
                (uid, when, agent_id, event, level, attributes),
            )
            return
        if event == "llm_usage" and category == "telemetry":
            fields = payload or {}
            source = fields.get("source")
            cost = fields.get("cost_usd")
            self._db.execute(
                """
                INSERT INTO llm_usage_ledger
                    (call_id, occurred_at, agent_id, machine, cluster, source, billing, usage_kind,
                     model, calls, complete, tokens_in, tokens_out, cache_read, cost_usd,
                     price_source, price_snapshot)
                VALUES (%s, %s, %s, 'test', %s, %s, %s, 'test', %s, %s, %s, %s, %s, %s, %s, %s, '{}')
                """,
                (
                    f"api:{uid}",
                    when,
                    agent_id,
                    cluster or cluster_label(meiva_home()),
                    source,
                    harness_billing_of_source(source if isinstance(source, str) else None).value,
                    fields.get("model", ""),
                    fields.get("calls", 1),
                    fields.get("complete", True),
                    _reported_count(fields.get("in_total")),
                    _reported_count(fields.get("out_total")),
                    _reported_count(fields.get("cache_read")),
                    cost,
                    "live" if cost is not None else None,
                ),
            )
        self._db.execute(
            "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
            "category, event_name, level, source, attributes, deployed_sha) VALUES (%s, %s, %s, 'test', %s, "
            "'test', %s, %s, %s, 'test', %s::jsonb, %s)",
            (
                uid,
                when,
                agent_id,
                cluster or cluster_label(meiva_home()),
                category,
                event,
                level,
                attributes,
                sha,
            ),
        )


@pytest.fixture
def event_rows(db_conn: psycopg.Connection) -> _EventRows:
    """The window's event rows: each test adds its own."""
    return _EventRows(db_conn)


def _insert_agent_row(db: psycopg.Connection, label: str = "t") -> int:
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None, "INSERT ... RETURNING must return a row"
    return row[0]


def _insert_agent(db: psycopg.Connection, *, status: str = "running", spawner: str = "user") -> int:
    tid = _insert_agent_row(db, f"agent-{spawner}")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, %s, %s)",
            (tid, spawner, status),
        )
    return tid


def test_dashboard_empty_db_returns_zeros(db_conn: psycopg.Connection) -> None:
    db_conn.commit()  # let truncate take effect before starting client
    with gateway_test_client() as client:
        resp = client.get("/api/stats/dashboard")
    assert resp.status_code == 200
    body = resp.json()
    assert "avg_turn_seconds" not in body
    assert {
        "live_count": body["live_count"],
        "window_hours": body["window_hours"],
        "tokens": body["tokens"],
        "cost_usd": body["cost_usd"],
        "subscription_cost_usd": body["subscription_cost_usd"],
        "openai_cost_usd": body["openai_cost_usd"],
        "anthropic_cost_usd": body["anthropic_cost_usd"],
        "google_cost_usd": body["google_cost_usd"],
        "alerts": body["alerts"],
    } == {
        "live_count": 0,
        "window_hours": 24,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_hit_pct": 0},
        "cost_usd": 0,
        "subscription_cost_usd": 0,
        "openai_cost_usd": 0,
        "anthropic_cost_usd": 0,
        "google_cost_usd": 0,
        "alerts": {"unresolved": 0, "critical": 0, "error": 0, "warning": 0},
    }


def _insert_live_alert(
    db: psycopg.Connection,
    *,
    fingerprint: str,
    alertname: str,
    severity: str,
    status: str = "unresolved",
    extra_labels: dict[str, str] | None = None,
) -> None:
    now = datetime.now(UTC)
    ends_at = now if status == "resolved" else None
    labels = {"alertname": alertname, "severity": severity, **(extra_labels or {})}
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO alerts (fingerprint, starts_at, ends_at, status, alertname, severity, "
            "labels, annotations, source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, 'grafana')",
            (
                fingerprint,
                now - timedelta(minutes=5),
                ends_at,
                status,
                alertname,
                severity,
                json.dumps(labels),
                json.dumps({"summary": alertname}),
            ),
        )


def test_dashboard_alerts_match_grafana_live_alerts_ssot(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    _insert_live_alert(db_conn, fingerprint="f-crit", alertname="db_down", severity="critical")
    _insert_live_alert(db_conn, fingerprint="f-err", alertname="llm_fail", severity="error")
    _insert_live_alert(db_conn, fingerprint="f-warn", alertname="high_lag", severity="warning")
    _insert_live_alert(
        db_conn, fingerprint="f-res", alertname="recovered", severity="critical", status="resolved"
    )
    _insert_live_alert(
        db_conn,
        fingerprint="f-wd",
        alertname="Alert pipeline watchdog (synthetic heartbeat)",
        severity="warning",
        extra_labels={"metric": "alert_pipeline_watchdog"},
    )
    event_rows.add(event="turn_end", level="error")
    db_conn.commit()
    with gateway_test_client() as client:
        dashboard = client.get("/api/stats/dashboard").json()
        alerts_api = client.get("/api/alerts").json()
    assert dashboard["alerts"] == {"unresolved": 3, "critical": 1, "error": 1, "warning": 1}
    assert dashboard["alerts"]["unresolved"] == alerts_api["meta"]["unresolved_count"]


def test_dashboard_live_count_excludes_terminated(db_conn: psycopg.Connection) -> None:
    """live_count = all non-terminated agents, running and idling."""
    _insert_agent(db_conn, status="running")
    _insert_agent(db_conn, status="idling")
    _insert_agent(db_conn, status="terminated")
    _insert_agent(db_conn, status="idling")
    db_conn.commit()
    with gateway_test_client() as client:
        resp = client.get("/api/stats/dashboard")
    assert resp.json()["live_count"] == 3


def test_dashboard_aggregates_llm_usage_payload(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """Windowed LLM token fields sum the corresponding Loki payload values."""
    event_rows.add(
        event="llm_usage",
        payload={"in_total": 1500, "out_total": 300, "cache_read": 1200, "cost_usd": 1.2},
    )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 1500
    assert body["tokens"]["output"] == 300
    assert body["tokens"]["cache_read"] == 1200
    # 1200 / 1500 = 80%
    assert body["tokens"]["cache_hit_pct"] == 80


def test_dashboard_cache_hit_pct_two_decimals(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """cache_hit_pct = cache_read / input * 100 with two decimal places (no longer integer truncation).
    1000 / 3000 = 33.333... → 33.33."""
    event_rows.add(
        event="llm_usage",
        payload={"in_total": 3000, "out_total": 100, "cache_read": 1000, "cost_usd": 1.2},
    )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["cache_hit_pct"] == 33.33


def test_dashboard_cost_uses_usage_time_snapshots(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """Cost sums telemetry snapshots, regardless of model registry state."""
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "claude-opus-4-8",
            "in_total": 1_000_000,
            "out_total": 1_000_000,
            "cache_read": 0,
            "cost_usd": 30.0,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "retired-model",
            "in_total": 999,
            "out_total": 999,
            "cache_read": 0,
            "cost_usd": 7.25,
        },
    )
    event_rows.add(
        event="llm_usage",
        category="log",
        payload={
            "model": "retired-log-model",
            "in_total": 1,
            "out_total": 1,
            "cache_read": 0,
            "cost_usd": 0.75,
        },
    )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    # The matching log event is excluded: llm_usage is telemetry-only in both
    # the status card and Grafana panels.
    assert body["cost_usd"] == pytest.approx(37.25)  # pyright: ignore[reportUnknownMemberType]
    assert math.isclose(body["openai_cost_usd"], 0.0)
    assert math.isclose(body["anthropic_cost_usd"], 30.0)
    assert math.isclose(body["google_cost_usd"], 0.0)


def test_dashboard_provider_api_costs_exclude_subscription_harnesses(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "gpt-6.1-sol",
            "in_total": 1000,
            "out_total": 500,
            "cache_read": 200,
            "cost_usd": 12.5,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "gpt-6-luna-decisions",
            "source": "memory_rerank",
            "in_total": 400,
            "out_total": 0,
            "cost_usd": 0.25,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "gpt-6-astra",
            "source": "codex",
            "in_total": 2000,
            "out_total": 800,
            "cost_usd": 9.0,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "claude-sonnet-5-5",
            "in_total": 1500,
            "out_total": 600,
            "cache_read": 500,
            "cost_usd": 6.75,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "claude-sonnet-5-5",
            "source": "claude_code",
            "in_total": 3000,
            "out_total": 900,
            "cost_usd": 14.0,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "gemini-decision-1",
            "source": "skill_recall",
            "in_total": 800,
            "out_total": 0,
            "cost_usd": 1.15,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "gemini-embedding-2",
            "in_total": 200,
            "out_total": 0,
            "cost_usd": 0.1,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "gemini-4-hydrogen",
            "source": "antigravity",
            "in_total": 4000,
            "out_total": 1000,
            "cost_usd": 11.0,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "deepseek-flash",
            "in_total": 500,
            "out_total": 200,
            "cost_usd": 1.5,
        },
    )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 13400
    assert body["tokens"]["output"] == 4000
    assert math.isclose(body["cost_usd"], 22.25)
    # meiva:note
    # codex 9.0 + claude_code 14.0 + antigravity 11.0: the same rows' subscription half, kept apart
    # from the metered API sum and its vendor slices (including google_cost_usd) rather than folded
    # into or dropped from them.
    # meiva:link gateway/cluster/docs/ops-surfaces.md
    assert math.isclose(body["subscription_cost_usd"], 34.0)
    assert math.isclose(body["openai_cost_usd"], 12.75)
    assert math.isclose(body["anthropic_cost_usd"], 6.75)
    assert math.isclose(body["google_cost_usd"], 1.25)


# meiva:note
# Either half of the cost split can be zero: a home that only runs subscription harnesses owes
# nothing to an API and still reports the list value it consumed, and the reverse. Both fields
# are served unconditionally so the console can render `$0.00` instead of hiding a half.
def test_dashboard_subscription_cost_is_a_disjoint_half(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "claude-sonnet-5-5",
            "source": "claude_code",
            "in_total": 100,
            "out_total": 20,
            "cost_usd": 2.5,
        },
    )
    event_rows.add(
        event="llm_usage",
        payload={
            "model": "gpt-6.1-sol",
            "source": "codex",
            "in_total": 100,
            "out_total": 20,
            "cost_usd": 1.25,
        },
    )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["cost_usd"] == 0
    assert math.isclose(body["subscription_cost_usd"], 3.75)


def test_dashboard_api_only_window_reports_zero_subscription_cost(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    event_rows.add(
        event="llm_usage",
        payload={"model": "deepseek-flash", "in_total": 100, "out_total": 20, "cost_usd": 0.5},
    )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert math.isclose(body["cost_usd"], 0.5)
    assert body["subscription_cost_usd"] == 0


def test_dashboard_default_24h_window_filters_old_rows(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    """Loki aggregates include only recent usage (the PG events table is gone
    since the #1823 cleanup — the stream lives in Loki)."""
    aid = _insert_agent(db_conn, status="running")
    event_rows.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 999, "out_total": 999, "cache_read": 0, "cost_usd": 99.9},
        ts_offset_hours=25,
    )
    event_rows.add(
        event="llm_usage",
        agent_id=aid,
        payload={"in_total": 10, "out_total": 5, "cache_read": 0, "cost_usd": 0.2},
        ts_offset_hours=1,
    )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 10
    assert body["tokens"]["output"] == 5


def _assert_provider_costs(
    body: dict[str, float], *, openai: float, anthropic: float, google: float
) -> None:
    assert math.isclose(body["openai_cost_usd"], openai)
    assert math.isclose(body["anthropic_cost_usd"], anthropic)
    assert math.isclose(body["google_cost_usd"], google)


def test_dashboard_hours_param_selects_window(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    aid = _insert_agent(db_conn, status="running")
    event_rows.add(event="delivery_stalled", level="warning", agent_id=aid, ts_offset_hours=3)
    event_rows.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "model": "gpt-6.1-sol",
            "in_total": 100,
            "out_total": 50,
            "cache_read": 0,
            "cost_usd": 1.0,
        },
        ts_offset_hours=3,
    )
    event_rows.add(
        event="llm_usage",
        agent_id=aid,
        payload={
            "model": "claude-sonnet-5-5",
            "in_total": 10,
            "out_total": 5,
            "cache_read": 0,
            "cost_usd": 0.1,
        },
        ts_offset_hours=0.5,
    )
    db_conn.commit()
    with gateway_test_client() as client:
        narrow = client.get("/api/stats/dashboard", params={"hours": 1}).json()
        wide = client.get("/api/stats/dashboard", params={"hours": 6}).json()
    assert narrow["window_hours"] == 1
    assert narrow["tokens"]["input"] == 10
    assert narrow["tokens"]["output"] == 5
    _assert_provider_costs(narrow, openai=0.0, anthropic=0.1, google=0.0)
    assert wide["window_hours"] == 6
    assert wide["tokens"]["input"] == 110
    assert wide["tokens"]["output"] == 55
    _assert_provider_costs(wide, openai=1.0, anthropic=0.1, google=0.0)


def test_five_minute_window_delta() -> None:
    assert window_delta(StatsWindowHours.M5) == timedelta(minutes=5)


def test_dashboard_all_whitelisted_hours_accepted(db_conn: psycopg.Connection) -> None:
    db_conn.commit()
    with gateway_test_client() as client:
        for hours in (0, 1, 6, 24, 72, 168, 720):
            resp = client.get("/api/stats/dashboard", params={"hours": hours})
            assert resp.status_code == 200
            assert resp.json()["window_hours"] == hours


@pytest.mark.parametrize("bad", ["5", "-1", "25", "169", "721", "abc", "24.5"])
def test_dashboard_invalid_hours_422(db_conn: psycopg.Connection, bad: str) -> None:
    db_conn.commit()
    with gateway_test_client() as client:
        resp = client.get("/api/stats/dashboard", params={"hours": bad})
    assert resp.status_code == 422


def test_dashboard_week_window_reads_the_whole_week_without_a_retention_clamp(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    event_rows.add(
        event="llm_usage", payload={"in_total": 10, "out_total": 5}, ts_offset_hours=24 * 6
    )
    event_rows.add(
        event="llm_usage", payload={"in_total": 100, "out_total": 50}, ts_offset_hours=24 * 8
    )
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard", params={"hours": 168}).json()
    assert body["applied_window_hours"] == 168
    assert body["tokens"]["input"] == 10
    assert body["tokens"]["output"] == 5


def test_dashboard_counts_the_home_cluster_and_unlabelled_rows_only(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    event_rows.add(event="boom", level="error")
    event_rows.add(event="boom", level="error", cluster="")
    event_rows.add(event="boom", level="error", cluster="some-other-cluster")
    event_rows.add(event="llm_usage", payload={"in_total": 7}, cluster="some-other-cluster")
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 0


def test_dashboard_sums_known_counts_in_partially_reported_usage(
    db_conn: psycopg.Connection, event_rows: _EventRows
) -> None:
    event_rows.add(event="llm_usage", payload={"in_total": 10, "cost_usd": 0.5})
    event_rows.add(event="llm_usage", payload={"in_total": "n/a", "cost_usd": None})
    event_rows.add(event="llm_usage", payload={})
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 10
    assert body["cost_usd"] == pytest.approx(0.5)


@pytest.mark.parametrize("cached", [None, 0, 800])
def test_dashboard_cache_rate_only_uses_metered_input(
    db_conn: psycopg.Connection, event_rows: _EventRows, cached: int | None
) -> None:
    event_rows.add(
        event="llm_usage",
        payload={
            "in_total": 1000,
            "cost_usd": 1,
            **({"cache_read": cached} if cached is not None else {}),
        },
    )
    event_rows.add(event="llm_usage", payload={"in_total": 5000, "cost_usd": 2})
    db_conn.commit()
    with gateway_test_client() as client:
        body = client.get("/api/stats/dashboard").json()
    assert body["tokens"]["input"] == 6000
    assert body["cost_usd"] == 3
    assert body["tokens"]["cache_hit_pct"] == (None if cached is None else cached / 10)
