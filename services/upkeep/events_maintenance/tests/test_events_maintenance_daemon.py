# meiva:note
# `services.upkeep.events_maintenance.daemon` against the throwaway Postgres.
#
# The hourly pass replays the JSONL mirror, rolls the closed days, folds the settled ones, samples the
# checkpoint tables and vacuums inside the quiet window; the service runs it beside the resolution and
# registry-gauge loops under one TaskGroup, so a loop that raises ends the process while a pass that
# overruns its deadline only fails health. Each of those is observed here on real rows, a real health
# endpoint and real database faults.

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool
from pydantic import SecretStr

import base.db
from base.clock import SYSTEM_TIMERS
from base.config import ConfigBoot
from base.config.domains.daemon.settings import DaemonSettings
from base.config.tests.configuration import snapshot_process_config
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import LivenessGroup, LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.delivery.mirror import event_id
from services.upkeep.events_maintenance import daemon
from services.upkeep.events_maintenance.tests.db_faults import table_locked, table_missing
from services.upkeep.events_maintenance.tests.service_probe import (
    assert_released,
    component_names,
    components,
    healthz,
    our_backends,
    wait_serving,
)
from services.upkeep.events_maintenance.tests.slices import events_maintenance_config
from services.upkeep.events_maintenance.tests.test_events_maintenance_resolution import (
    record_maintenance_event,
)

# meiva:note
# 13:00 in America/Los_Angeles: outside the vacuum window, so a pass never dials a direct connection.
_NOW = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)
_MIRRORED_AT = "2026-10-02T12:00:00+00:00"


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=4, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


def _agent(db: psycopg.Connection) -> int:
    row = db.execute("INSERT INTO agents (label) VALUES ('rolled') RETURNING id").fetchone()
    assert row is not None
    db.commit()
    return int(row[0])


def _mirror_line(agent_id: int, marker: str) -> bytes:
    body: dict[str, Any] = {
        "ts": _MIRRORED_AT,
        "trace_id": None,
        "span_id": None,
        "agent_id": agent_id,
        "machine": marker,
        "cluster": "c",
        "process": "p",
        "category": "telemetry",
        "event_name": "llm_usage",
        "level": "info",
        "source": "system",
        "target_agent_id": None,
        "attributes": {"in_total": 5, "out_total": 2, "model": "m"},
    }
    text = json.dumps(body, separators=(",", ":"))
    body["id"] = event_id(text, int(1_790_000_000 * 1e9))
    return (json.dumps(body, separators=(",", ":")) + "\n").encode()


def _pass(
    pool: ConnectionPool,
    database: Database,
    process_config: ConfigBoot,
    mirror_dir: Path,
    progress: LoopProgress,
) -> None:
    daemon.run_maintenance(
        pool,
        progress,
        events_maintenance_config(process_config, mirror_dir=mirror_dir),
        database,
        now=_NOW,
        host="local",
    )


def _ledger(db: psycopg.Connection, agent_id: int) -> list[tuple[Any, ...]]:
    db.commit()
    return db.execute(
        "SELECT day, model, llm_calls, tokens_in, tokens_out FROM agent_model_tokens_daily "
        "WHERE agent_id = %s",
        (agent_id,),
    ).fetchall()


def test_the_pass_replays_the_mirror_rolls_the_closed_days_and_marks_success(
    db_conn: psycopg.Connection,
    database: Database,
    pool: ConnectionPool,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    agent = _agent(db_conn)
    (tmp_path / "events-20261002.jsonl").write_bytes(_mirror_line(agent, "mirrored"))
    progress = LoopProgress("dispatch", timeout_s=60.0, timers=SYSTEM_TIMERS)

    _pass(pool, database, process_config, tmp_path, progress)

    stored = db_conn.execute(
        "SELECT event_name FROM telemetry_events WHERE machine = 'mirrored'"
    ).fetchall()
    assert stored == [("llm_usage",)]
    assert _ledger(db_conn, agent) == [(_NOW.date().replace(day=2), "m", 1, 5, 2)]
    assert progress.snapshot()["last_success_at"] is not None


def test_a_failing_replay_does_not_stop_the_rest_of_the_pass(
    db_conn: psycopg.Connection,
    database: Database,
    pool: ConnectionPool,
    process_config: ConfigBoot,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    agent = _agent(db_conn)
    (tmp_path / "events-20261002.jsonl").write_bytes(b"this is not an event\n")
    db_conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (1, %s, %s, 'm', 'c', 'p', "
        "'telemetry', 'llm_usage', 'info', 'test', %s::jsonb)",
        (_MIRRORED_AT, agent, json.dumps({"in_total": 7, "out_total": 1, "model": "m"})),
    )
    db_conn.commit()
    progress = LoopProgress("dispatch", timeout_s=60.0, timers=SYSTEM_TIMERS)

    _pass(pool, database, process_config, tmp_path, progress)

    assert "telemetry events replay incomplete" in caplog.text
    assert _ledger(db_conn, agent) == [(_NOW.date().replace(day=2), "m", 1, 7, 1)]
    assert progress.snapshot()["last_success_at"] is not None


# meiva:note
# An event the live projection missed stays absent from `agent_metric_observations` however
# many maintenance passes run: nothing re-derives observations from `telemetry_events`.
def test_the_pass_does_not_rebuild_observations_the_live_projection_missed(
    db_conn: psycopg.Connection,
    database: Database,
    pool: ConnectionPool,
    process_config: ConfigBoot,
    tmp_path: Path,
) -> None:
    agent = _agent(db_conn)
    db_conn.execute(
        "INSERT INTO telemetry_events (event_uid, ts, agent_id, machine, cluster, process, "
        "category, event_name, level, source, attributes) VALUES (1, now(), %s, 'm', 'c', 'p', "
        "'telemetry', 'llm_usage', 'info', 'test', %s::jsonb)",
        (agent, json.dumps({"in_total": 5, "out_total": 2, "cost_usd": 0.25})),
    )
    db_conn.commit()

    for _ in range(2):
        _pass(
            pool,
            database,
            process_config,
            tmp_path,
            LoopProgress("dispatch", timeout_s=60.0, timers=SYSTEM_TIMERS),
        )

    assert db_conn.execute("SELECT count(*) FROM agent_metric_observations").fetchone() == (0,)


def test_loop_components_report_fresh_loops_as_ok() -> None:
    liveness = LivenessGroup(timers=SYSTEM_TIMERS)
    liveness.register("dispatch", timeout_s=15.0)
    liveness.register("resolution", timeout_s=20.0)

    assert daemon.loop_components(
        liveness, {"dispatch": 15.0, "resolution": 20.0}, now=time.time()
    ) == [
        {"name": "dispatch", "status": "ok", "progress": "idle"},
        {"name": "resolution", "status": "ok", "progress": "idle"},
    ]


def test_loop_components_report_a_wedged_loop_and_why() -> None:
    liveness = LivenessGroup(timers=SYSTEM_TIMERS)
    dispatch = liveness.register("dispatch", timeout_s=15.0)
    dispatch.fail("dispatch pass exceeded hard deadline")

    assert daemon.loop_components(liveness, {"dispatch": 15.0}, now=time.time()) == [
        {
            "name": "dispatch",
            "status": "degraded",
            "last_error": "dispatch pass exceeded hard deadline",
            "progress": "wedged",
            "detail": "wedged: dispatch pass exceeded hard deadline",
        }
    ]


def test_loop_components_carry_the_age_of_the_last_success() -> None:
    liveness = LivenessGroup(timers=SYSTEM_TIMERS)
    dispatch = liveness.register("dispatch", timeout_s=15.0)
    dispatch.mark_success()
    stamped = dispatch.snapshot()["last_success_at"]
    assert isinstance(stamped, str)
    last_success = datetime.fromisoformat(stamped).timestamp()

    assert daemon.loop_components(liveness, {"dispatch": 15.0}, now=last_success + 12.5) == [
        {
            "name": "dispatch",
            "status": "ok",
            "last_success": last_success,
            "age_s": 12.5,
            "progress": "idle",
        }
    ]


def test_deadline_settings_defaults_and_env_aliases() -> None:
    defaults = DaemonSettings()
    assert defaults.events_maintenance_pass_deadline_s == 1500.0
    assert defaults.events_maintenance_resolution_deadline_s == 600.0

    configured = DaemonSettings.model_validate(
        {
            "MEIVA_EVENTS_MAINTENANCE_PASS_DEADLINE_S": "15.5",
            "MEIVA_EVENTS_MAINTENANCE_RESOLUTION_DEADLINE_S": "35.5",
        }
    )
    assert configured.events_maintenance_pass_deadline_s == 15.5
    assert configured.events_maintenance_resolution_deadline_s == 35.5


def _boot(**fields: Any) -> ConfigBoot:
    boot = snapshot_process_config()
    for name, value in fields.items():
        boot.set_field(name, value)
    return boot


def _run(boot: ConfigBoot, database: Database, tmp_path: Path) -> asyncio.Task[None]:
    return asyncio.create_task(
        daemon.run(
            process_config=boot,
            runtime=tmp_path / "run",
            database=lambda: database,
            image=LoadedCommit(Path(), None),
        )
    )


def _port(boot: ConfigBoot, tmp_path: Path) -> int:
    return (
        ServiceEndpoints.from_boot(boot, runtime=tmp_path / "run")
        .of("events_maintenance")
        .health_port
    )


# meiva:note
# The alert reconciliation loop exists only on a unit holding the Grafana credential.
@pytest.mark.parametrize("credential", [True, False])
async def test_the_health_endpoint_lists_the_loops_the_unit_runs(
    db_conn: psycopg.Connection, database: Database, tmp_path: Path, *, credential: bool
) -> None:
    boot = _boot(grafana_admin_password=SecretStr("grafana-test-password") if credential else None)
    port = _port(boot, tmp_path)
    service = _run(boot, database, tmp_path)
    try:
        await wait_serving(port, tmp_path / "run" / "events_maintenance.pid")
        status, body = await asyncio.to_thread(healthz, port)
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)

    assert status == 200
    expected = ["dispatch", "resolution", "registry_gauge"]
    assert component_names(body) == expected + (["alert_reconciliation"] if credential else [])
    await assert_released(db_conn, tmp_path / "run" / "events_maintenance.pid", port)


# meiva:note
# The loops share one TaskGroup: one that raises cancels the others and `run` leaves with the
# error, so the supervisor restarts the service.
@pytest.mark.parametrize(
    ("crashing", "table"),
    [
        ("dispatch", "agent_model_tokens_total_through"),
        ("resolution", "event_dismissals"),
    ],
)
async def test_a_crashing_loop_cancels_its_siblings_and_ends_the_service(
    db_conn: psycopg.Connection,
    database: Database,
    tmp_path: Path,
    crashing: str,
    table: str,
) -> None:
    record_maintenance_event(db_conn, level="info", event_name="heartbeat", minutes_ago=0.1)
    boot = _boot()
    port = _port(boot, tmp_path)
    before = asyncio.all_tasks()

    with table_missing(db_conn, table), pytest.raises(ExceptionGroup) as raised:
        await asyncio.wait_for(
            daemon.run(
                process_config=boot,
                runtime=tmp_path / "run",
                database=lambda: database,
                image=LoadedCommit(Path(), None),
            ),
            timeout=60.0,
        )

    assert [type(exc) for exc in raised.value.exceptions] == [psycopg.errors.UndefinedTable], (
        crashing
    )
    assert asyncio.all_tasks() == before, "a sibling loop was left running"
    await assert_released(db_conn, tmp_path / "run" / "events_maintenance.pid", port)


# meiva:note
# A worker thread blocked in the database past its deadline fails health, and stopping the
# service cancels its proxy instead of joining the thread.
async def test_stopping_the_service_does_not_wait_for_a_pass_past_its_deadline(
    db_conn: psycopg.Connection, database: Database, db_url: str, tmp_path: Path
) -> None:
    boot = _boot(events_maintenance_pass_deadline_s=0.5)
    port = _port(boot, tmp_path)
    pidfile = tmp_path / "run" / "events_maintenance.pid"
    before = asyncio.all_tasks()

    with table_locked(db_url, "telemetry_events"):
        service = _run(boot, database, tmp_path)
        try:
            await wait_serving(port, pidfile)
            deadline = time.monotonic() + 30.0
            while True:
                status, body = await asyncio.to_thread(healthz, port)
                dispatch = next(c for c in components(body) if c["name"] == "dispatch")
                if dispatch["progress"] == "wedged":
                    break
                assert time.monotonic() < deadline, "the pass never failed health"
                await asyncio.sleep(0.05)
            assert status == 503

            service.cancel()
            await asyncio.wait_for(asyncio.gather(service, return_exceptions=True), timeout=30.0)
        finally:
            service.cancel()

        assert service.cancelled()
        assert not pidfile.exists()
        assert asyncio.all_tasks() - before == set(), "the stopped service left a proxy alive"
        assert our_backends(db_conn) > 0, "the blocked worker is still waiting on its lock"

    await assert_released(db_conn, pidfile, port)
