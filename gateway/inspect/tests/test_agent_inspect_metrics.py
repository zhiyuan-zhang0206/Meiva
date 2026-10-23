"""Integration tests for GET /api/agents/{id}/inspect/metrics (W13b).

The inspector surface of the plugin metric system: the gateway builds the
metric registry in process (task #180 PR D — mocked here by patching
`load_plugin_metrics`), keeps `output`-inspector metrics, renders each
template for the agent, re-validates the rendered query, substitutes the
Grafana time macros with a fixed window, and executes the query — LogQL
against a fake Loki client, SQL against the test DB.

Locks: empty registry -> [], unknown agent -> 404, tampered template -> 500
with the reason, {{agent_id}} template rendered with no agent id -> 400,
execution-time query failure -> per-metric `error` while the other metrics
still render, and the execution semantics (event_name/category literals,
per-agent filtering, stat vs series payloads, macro translation).
"""

from __future__ import annotations

import json
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi import HTTPException
from psycopg_pool import ConnectionPool

import meiva_builtins.plugins as shipped_plugins
from base.db import Database
from base.telemetry.metrics.plugin_metrics import MetricSpec
from gateway.inspect import _plugin_metrics
from gateway.inspect._plugin_faces import SHIPPED_PLUGINS_DIR, enabled_plugin_names
from gateway.inspect._plugin_metrics import (
    metrics_for_agent,
    render_metric_query,
    translate_macros,
)

# A valid inspector query — the static-SQL shape (task #180 PR C): the
# template era (macros + {event_name}/{category}/{{agent_id}} placeholders)
# is over; the per-agent inspector idiom lives in the LogQL dialect. A
# timeseries SQL metric is no longer expressible (bucketing was a macro
# feature), so the SQL inspector surface is stat-shaped (core_live_agents).
# Post-#1823 the SQL metric dialect reads live tables only (the frozen
# `events` archive was dropped); `agents_meta` is the surviving live shape
# (see core_live_agents). The demo/stat queries below are agents_meta-based.
_DEMO_QUERY = (
    "SELECT 100.0 * count(*) FILTER (WHERE status = 'running') "
    '/ NULLIF(count(*), 0) AS "running %" '
    "FROM agents_meta"
)

# A stat-shaped inspector query (one aggregate row).
_STAT_QUERY = "SELECT count(*) AS live FROM agents_meta WHERE status IN ('running', 'idling')"


@pytest.fixture
def pool(database: Database) -> Iterator[ConnectionPool]:
    """A real isolated database, borrowed the way the route borrows it."""
    with database.pool(max_size=2) as opened:
        yield opened


def _metric(
    name: str = "demo_task_done_rate",
    *,
    event_name: str = "task_update",
    category: str = "audit",
    panel: str = "stat",
    query: str = _DEMO_QUERY,
    output: list[str] | None = None,
    unit: str = "percent",
) -> dict[str, Any]:
    """A registry-row dict (the shape a MetricSpec registration carries)."""
    return {
        "name": name,
        "title": "Task done rate",
        "description": "test metric",
        "event_name": event_name,
        "category": category,
        "unit": unit,
        "panel": panel,
        "query": query,
        "output": output or ["inspector"],
        "plugin": "test_plugin",
    }


# meiva:note
# The inspector results of `agent_id` for these registry rows, as the route serializes them.
def _run(pool: ConnectionPool, agent_id: int, *metrics: dict[str, Any]) -> list[dict[str, Any]]:
    registry = [MetricSpec.model_validate(m) for m in metrics]
    results = metrics_for_agent(pool, agent_id, registry=registry)
    return [result.model_dump(mode="json") for result in results]


def _insert_agent(db: psycopg.Connection, label: str = "t") -> int:
    """INSERT an agents row + its agents_meta row (the /inspect family checks
    agents_meta; a bare `agents` row would 404)."""
    with db.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES (%s) RETURNING id", (label,))
        row = cur.fetchone()
    assert row is not None
    tid = row[0]
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO agents_meta (id, spawner, status) VALUES (%s, 'user', 'running')",
            (tid,),
        )
    return tid


# ── file absent / agent unknown ───────────────────────────────────────────────


# meiva:note
# No registered metrics -> [].
def test_metrics_empty_registry_returns_empty(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()
    assert _run(pool, aid) == []


def test_metrics_unknown_agent_404(pool: ConnectionPool, db_conn: psycopg.Connection) -> None:
    """No agents_meta row -> 404 (same contract as /inspect)."""
    db_conn.commit()
    with pytest.raises(HTTPException) as caught:
        _run(pool, 999999, _metric())
    assert caught.value.status_code == 404


# ── filtering + rendering + execution ─────────────────────────────────────────


def test_metrics_filters_inspector_output(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """Only metrics whose output includes 'inspector' are returned; a
    grafana-only metric (even one sharing the same table shape) is skipped."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    body = _run(
        pool,
        aid,
        _metric(name="insp_metric"),
        _metric(
            name="grafana_only",
            query="SELECT count(*) FROM agents_meta WHERE event_name = {event_name} AND category = {category}",
            output=["grafana"],
        ),
    )
    assert [m["name"] for m in body] == ["insp_metric"]


def test_metrics_stat_scalar(pool: ConnectionPool, db_conn: psycopg.Connection) -> None:
    """A stat metric returns the single aggregate as `value`. The SQL
    inspector surface is static now (task #180 PR C — no macro windows), so
    the query counts exactly what its own predicates select."""
    aid = _insert_agent(db_conn)
    _insert_agent(db_conn)
    db_conn.commit()
    body = _run(pool, aid, _metric(name="stat_count", panel="stat", query=_STAT_QUERY))
    assert len(body) == 1
    assert body[0]["name"] == "stat_count"
    assert body[0]["panel"] == "stat"
    assert body[0]["value"] == 2
    assert body[0]["series"] == []
    assert body[0]["error"] is None


def test_metrics_macro_translation_unit() -> None:
    """The Grafana macros translate to the fixed inspector window; macro
    arguments (file-controlled) are consumed wholesale and never reach the
    output."""
    sql = translate_macros(
        "SELECT $__timeGroup(ts, $__interval) AS time, count(*) "
        "FROM agents_meta WHERE event_name = 'k' AND $__timeFilter(ts) "
        "AND $__timeFilter(ts, 'extra')"
    )
    # the double AT TIME ZONE round-trip truncates in UTC while keeping the
    # bucket column a tz-aware timestamptz (see _MACRO_TIMEGROUP)
    assert "date_trunc('hour', ts AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'" in sql
    assert "ts >= now() - interval '24 hours'" in sql
    assert "$__" not in sql
    # a weird argument inside the macro parens is discarded, not echoed
    sql2 = translate_macros(
        "SELECT count(*) FROM agents_meta WHERE $__timeFilter(attributes->>'model')"
    )
    assert "attributes" not in sql2
    assert sql2.count("$__timeFilter") == 0


# ── safety re-validation (tampered file) ──────────────────────────────────────


@pytest.mark.parametrize(
    "tampered",
    [
        # DML sneaked past the generator
        "SELECT count(*) FROM agents_meta; DELETE FROM agents_meta",
        # different FROM target
        "SELECT count(*) FROM agents WHERE id = 1",
        # denied function call
        "SELECT pg_sleep(1) FROM agents_meta",
        # comment-injected
        "SELECT count(*) FROM agents_meta -- WHERE event_name = 'x'",
    ],
)
def test_metrics_tampered_query_500(
    pool: ConnectionPool, db_conn: psycopg.Connection, tampered: str
) -> None:
    """A registry file edited after generation fails the second validation ->
    500 with the reason (never executed)."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with pytest.raises(HTTPException) as caught:
        _run(pool, aid, _metric(name="tampered", query=tampered), _metric(name="still_fine"))
    assert caught.value.status_code == 500
    assert "re-validation" in caught.value.detail


def test_metrics_render_requires_agent_id_400() -> None:
    """A {{agent_id}} template rendered without an agent id is refused with
    400. (The HTTP route always carries the id in the path, so this is a
    helper-level guard — defense in depth against a future caller.) The
    {{agent_id}} idiom lives in the LogQL dialect (task #180 PR C)."""
    spec = MetricSpec.model_validate(_logql_metric())
    with pytest.raises(HTTPException) as exc_info:
        render_metric_query(spec, agent_id=None)
    assert exc_info.value.status_code == 400


def test_metrics_render_static_sql_passes_through() -> None:
    """A static SQL query renders verbatim and passes the whitelist (the
    template era is over, task #180 PR C)."""
    spec = MetricSpec.model_validate(_metric())
    query = render_metric_query(spec, agent_id=7)
    assert query == _DEMO_QUERY
    assert "{event_name}" not in query


# ── execution-time failure is per-metric, not fatal ───────────────────────────


# meiva:note
# A query that passes the whitelist but fails at runtime (a bad cast on
# the row values) lands in that metric's `error`; the sibling metric still
# renders.
def test_metrics_runtime_query_error_per_metric(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()
    body = _run(
        pool,
        aid,
        _metric(
            name="broken",
            query=("SELECT status::bigint AS n FROM agents_meta WHERE id = %(agent_id)s"),
        ),
        _metric(name="healthy", panel="stat", query=_STAT_QUERY),
    )
    by_name = {m["name"]: m for m in body}
    assert "bigint" in by_name["broken"]["error"].lower()
    assert by_name["healthy"]["error"] is None
    assert by_name["healthy"]["value"] == 1.0  # the seeded agents_meta row


# meiva:note
# The read-only enforcement must NOT leak onto pooled connections.
#
# The read opens its transaction with `SET TRANSACTION READ ONLY`
# (server-enforced, transaction-scoped) instead of `Connection.read_only` —
# that attribute is client-side state that persists on the pooled
# connection object after return, so a later borrower could inherit a
# read-only session and its writes would fail. After a metrics read,
# the same pool must still accept writes.
def test_metrics_read_only_is_transaction_scoped(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    aid = _insert_agent(db_conn)
    db_conn.commit()
    body = _run(pool, aid, _metric(name="stat_count", panel="stat", query=_STAT_QUERY))
    assert body[0]["value"] == 1
    # meiva:note
    # Same pool the read used — a write must succeed on a fresh borrow.
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO agents (label) VALUES ('post-metrics-write')")


# ── LogQL inspector metrics (task #1280) ──────────────────────────────────────


def _logql_metric(
    name: str = "agent_llm_cost",
    *,
    panel: str = "timeseries",
    query: str | None = None,
    output: list[str] | None = None,
) -> dict[str, Any]:
    """A registry-row dict for a logql inspector metric."""
    return {
        "name": name,
        "title": "Agent LLM cost",
        "description": "logql inspector metric",
        "event_name": "llm_usage",
        "category": "telemetry",
        "unit": "currencyUSD",
        "panel": panel,
        "query": query
        or (
            'sum(sum_over_time({service_name="unknown_service", event_name={event_name}} | json | '
            "category={category} | {{agent_id}} | "
            "unwrap attributes_cost_usd [$__interval]))"
        ),
        "output": output or ["inspector"],
        "plugin": "test_plugin",
        "query_type": "logql",
    }


def _usage(db: psycopg.Connection, agent_id: int, cost: float, *, minutes_ago: float) -> None:
    db.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) "
        "VALUES (%s, now() - (%s * interval '1 minute'), %s, 'm', 'c', 'p', 'telemetry', "
        "'llm_usage', 'info', 'test', %s::jsonb)",
        (uuid.uuid4().int % (1 << 62), minutes_ago, agent_id, json.dumps({"cost_usd": cost})),
    )


def test_metrics_logql_timeseries_is_answered_from_telemetry_events(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """A logql inspector metric is evaluated on Postgres over the fixed 24h window in 1h
    steps (the range-vector window ends at each step), and folds into the same
    PluginMetricResult shape. Another agent's rows stay out."""
    aid = _insert_agent(db_conn)
    other = _insert_agent(db_conn, "other")
    _usage(db_conn, aid, 0.25, minutes_ago=30)
    _usage(db_conn, aid, 0.5, minutes_ago=90)
    _usage(db_conn, other, 9.0, minutes_ago=30)
    db_conn.commit()
    [m] = _run(pool, aid, _logql_metric())
    assert m["error"] is None and m["value"] is None
    values = [point["value"] for point in m["series"]]
    assert len(values) == 25  # 24 hours of hourly steps, both ends
    assert sum(values) == pytest.approx(0.75)
    assert sorted(v for v in values if v)[-1] == 0.5


def test_metrics_logql_stat_is_the_last_step_over_the_whole_range(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """A stat-shaped logql metric returns the last step as `value`; `$__range` is 24h."""
    aid = _insert_agent(db_conn)
    _usage(db_conn, aid, 0.25, minutes_ago=30)
    _usage(db_conn, aid, 0.5, minutes_ago=600)
    _usage(db_conn, aid, 4.0, minutes_ago=60 * 30)  # outside the 24h range
    db_conn.commit()
    m = _run(
        pool,
        aid,
        _logql_metric(
            name="stat_cost",
            panel="stat",
            query=(
                'sum(sum_over_time({service_name="unknown_service", event_name={event_name}} | json | '
                "category={category} | {{agent_id}} | "
                "unwrap attributes_cost_usd [$__range]))"
            ),
        ),
    )[0]
    assert m["panel"] == "stat"
    assert m["value"] == pytest.approx(0.75)
    assert m["series"] == []


def test_metrics_logql_outside_the_evaluator_vocabulary_is_per_metric(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """A template the evaluator does not understand lands in the metric's `error` field;
    sibling metrics still render."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    body = {
        m["name"]: m
        for m in _run(
            pool,
            aid,
            _logql_metric(
                name="rate_query",
                query=(
                    'rate({service_name="unknown_service", event_name={event_name}} | json | '
                    "category={category} [1h])"
                ),
            ),
            _metric(name="still_fine"),
        )
    }
    assert "query failed" in body["rate_query"]["error"]
    assert body["still_fine"]["error"] is None


def test_metrics_logql_tampered_query_500(
    pool: ConnectionPool, db_conn: psycopg.Connection
) -> None:
    """A tampered logql registry row (lost selector / json pipeline) fails
    the rendered-form re-validation -> 500, never executed."""
    aid = _insert_agent(db_conn)
    db_conn.commit()
    with pytest.raises(HTTPException) as caught:
        _run(
            pool,
            aid,
            _logql_metric(
                name="tampered_logql",
                query='sum(count_over_time({other="x"} | json | event_name={event_name} [1h]))',
            ),
        )
    assert caught.value.status_code == 500
    assert "re-validation" in caught.value.detail


def test_metrics_logql_macro_translation_unit() -> None:
    """LogQL macros translate to the fixed window: $__interval -> 1h
    (LogQL duration syntax — '1 hour' is not a duration), $__range -> 24h."""
    translated = translate_macros(
        'sum(count_over_time({service_name="unknown_service"} | json | '
        'event_name="x" [$__interval])) + sum(sum_over_time({service_name="unknown_service"} | json | '
        'event_name="x" | unwrap attributes_cost_usd [$__range]))',
        logql=True,
    )
    assert "[1h]" in translated
    assert "[24h]" in translated
    assert "$__" not in translated


# ── in-process registry (task #180 PR D) ──────────────────────────────────────


def test_in_process_loader_imports_shipped_metrics() -> None:
    """The loader imports every shipped plugin metrics.py, admits its
    `contribute()` declaration into a data registry plus the core definition
    modules — plugin metrics first, then core, the old snapshot's two-section
    order. No file involved."""
    specs = _plugin_metrics.load_plugin_metrics(SHIPPED_PLUGINS_DIR, enabled_plugin_names())

    plugin_specs = [s for s in specs if s.plugin != "core"]
    core_specs = [s for s in specs if s.plugin == "core"]
    # the shipped plugin metrics (11, including recall-filter latency panels)
    assert {s.plugin for s in plugin_specs} == {"meiva_fleet", "meiva_memory", "meiva_syntax_fix"}
    assert len(plugin_specs) == 11
    # core section follows, plugin section first (old snapshot order)
    assert [s.plugin for s in specs].index("core") == len(plugin_specs)
    assert len(core_specs) >= 16
    # a second call rebuilds the same registry (no process-global state to double up)
    assert _plugin_metrics.load_plugin_metrics(SHIPPED_PLUGINS_DIR, enabled_plugin_names()) == specs


# meiva:note
# Metrics follow the enable rule the widgets already follow: a disabled shipped plugin
# contributes none (the per-agent inspector panel), read per request from the machine's
# plugin config.
def test_a_disabled_plugins_metrics_are_not_loaded(unit_home: Path) -> None:
    shipped = SHIPPED_PLUGINS_DIR / "meiva_fleet" / "metrics.py"
    assert shipped in _plugin_metrics.plugin_metric_modules(
        SHIPPED_PLUGINS_DIR, enabled_plugin_names()
    )
    (unit_home / "plugins_config.json").write_text(
        json.dumps({"plugins": {"meiva_fleet": {"enabled": False}}}), encoding="utf-8"
    )
    enabled = enabled_plugin_names()
    assert "meiva_fleet" not in enabled
    assert shipped not in _plugin_metrics.plugin_metric_modules(SHIPPED_PLUGINS_DIR, enabled)
    assert "meiva_fleet" not in {
        s.plugin for s in _plugin_metrics.load_plugin_metrics(SHIPPED_PLUGINS_DIR, enabled)
    }


def _fixture_plugin(
    root: Path, name: str, *, metric: str = "", raises: bool = False, query: str = _STAT_QUERY
) -> Path:
    """Write `<root>/<name>/metrics.py` declaring one inspector metric (and
    optionally raising on import); returns the file."""
    plugin_dir = root / name
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text("", encoding="utf-8")
    (plugin_dir / "meiva-plugin.json").write_text(
        json.dumps(
            {
                "apiVersion": 2,
                "name": name,
                "version": "1.0.0",
                "engines": {"meiva": ">=2026.10.1"},
                "contributions": {"metrics": [metric or f"{name}_one"]},
            }
        ),
        encoding="utf-8",
    )
    metrics_py = plugin_dir / "metrics.py"
    metrics_py.write_text(
        _fixture_metrics_source(metric or f"{name}_one", query, raises=raises), encoding="utf-8"
    )
    return metrics_py


def _fixture_metrics_source(metric: str, query: str, *, raises: bool = False) -> str:
    source = (
        "from base.packages.plugins.extensions import PluginContributions\n"
        "from base.telemetry.metrics.plugin_metrics import MetricSpec\n"
        f"METRICS = (MetricSpec(name={metric!r}, title='Fixture', "
        f"event_name='task_update', category='audit', output=['inspector'], query={query!r}),)\n"
        "def contribute():\n"
        "    return PluginContributions(metrics=METRICS)\n"
    )
    return ("raise RuntimeError('metrics boom')\n" if raises else "") + source


def _load_fixture_metrics(plugins_dir: Path) -> list[MetricSpec]:
    enabled = {p.name for p in plugins_dir.iterdir()}
    return _plugin_metrics.load_plugin_metrics(plugins_dir, enabled)


@pytest.fixture
def fixture_plugins_dir(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    """Point the loader at `tmp_path` as the shipped-plugins directory (the
    fixture plugins are the only ones it sees besides the core metrics)."""
    shipped_plugins.__path__.append(str(tmp_path))

    def _forget_fixture_modules() -> None:
        shipped_plugins.__path__.remove(str(tmp_path))
        for module_name in [m for m in sys.modules if m.startswith("meiva_builtins.plugins.fx_")]:
            del sys.modules[module_name]

    request.addfinalizer(_forget_fixture_modules)
    return tmp_path


def test_in_process_loader_skips_a_failing_module_and_recovers(
    fixture_plugins_dir: Path, loguru_records: list[dict[str, Any]]
) -> None:
    """A metrics.py that raises is reported and contributes nothing, while the
    rest still load; a declaration is admitted whole or not at all, so
    retrying the fixed file on the next call registers cleanly (fail-soft,
    user ruling 2026-09-11)."""
    metrics_py = _fixture_plugin(fixture_plugins_dir, "fx_drop_partial", raises=True)

    _load_fixture_metrics(fixture_plugins_dir)  # must not raise
    specs = _load_fixture_metrics(fixture_plugins_dir)
    assert [s for s in specs if s.plugin == "fx_drop_partial"] == []
    assert {s.plugin for s in specs if s.plugin != "core"} == set()
    assert any(
        "fx_drop_partial" in r["message"] and "failed to load" in r["message"]
        for r in loguru_records
    )

    metrics_py.write_text(
        _fixture_metrics_source("fx_drop_partial_one", _STAT_QUERY), encoding="utf-8"
    )
    specs = _load_fixture_metrics(fixture_plugins_dir)
    assert [(s.plugin, s.name) for s in specs if s.plugin == "fx_drop_partial"] == [
        ("fx_drop_partial", "fx_drop_partial_one")
    ]


def test_in_process_loader_refuses_a_plugin_claiming_a_taken_metric_name(
    fixture_plugins_dir: Path,
) -> None:
    """Metric names are global: a plugin declaring a name another plugin
    already holds is refused whole — none of its metrics serve and the first
    claimant is untouched."""
    _fixture_plugin(fixture_plugins_dir, "fx_first", metric="fx_shared_name")
    # Plugins load in sorted order, so `fx_first` is the first claimant.
    _fixture_plugin(fixture_plugins_dir, "fx_thief", metric="fx_shared_name")

    specs = _load_fixture_metrics(fixture_plugins_dir)
    assert [s.plugin for s in specs if s.name == "fx_shared_name"] == ["fx_first"]
    assert [s for s in specs if s.plugin == "fx_thief"] == []
