# meiva:note
# `services.upkeep.events_maintenance.daemon` against the throwaway Postgres.
#
# The hourly pass audits unknown writers, samples the checkpoint tables
# and vacuums inside the quiet window; the service runs it beside the resolution and
# registry-gauge loops under one TaskGroup, so a loop that raises ends the process while a pass that
# overruns its deadline only fails health. Each of those is observed here on real rows, a real health
# endpoint and real database faults.

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

import base.db
from base.clock.tests.fakes import system_clock
from base.config import ConfigBoot
from base.config.domains.daemon.settings import DaemonSettings
from base.config.tests.configuration import snapshot_process_config
from base.daemon.endpoints import ServiceEndpoints
from base.daemon.health import LivenessGroup, LoopProgress
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.tests.pool_probe import PoolProbe
from base.db.tests.pool_probe import pool_probe as pool_probe
from base.native_process.loaded_commit import LoadedCommit
from services.upkeep.events_maintenance import daemon
from services.upkeep.events_maintenance.tests.db_faults import table_locked, table_missing
from services.upkeep.events_maintenance.tests.service_probe import (
    assert_released,
    component_names,
    components,
    healthz,
    wait_serving,
)
from services.upkeep.events_maintenance.tests.slices import events_maintenance_config

# meiva:note
# 13:00 in America/Los_Angeles: outside the vacuum window, so a pass never dials a direct connection.
_NOW = datetime(2026, 10, 3, 20, 0, tzinfo=UTC)
_LEDGER_AT = "2026-10-02T12:00:00+00:00"


@pytest.fixture
def pool(db_url: str, *, database_gate: ProcessDbGate) -> Iterator[ConnectionPool]:
    p = base.db.pool(max_size=4, gate=database_gate)
    try:
        yield p
    finally:
        p.close()


def _pass(
    pool: ConnectionPool,
    database: Database,
    process_config: ConfigBoot,
    progress: LoopProgress,
) -> None:
    daemon.run_maintenance(
        pool, progress, events_maintenance_config(process_config), database, now=_NOW
    )


def test_the_pass_marks_success(
    database: Database, pool: ConnectionPool, process_config: ConfigBoot
) -> None:
    progress = LoopProgress("dispatch", timeout_s=60.0, clock=system_clock())

    _pass(pool, database, process_config, progress)

    assert progress.snapshot()["last_success_at"] is not None


def test_the_pass_prices_an_unpriced_ledger_row_at_its_own_time_and_leaves_priced_rows(
    db_conn: psycopg.Connection,
    database: Database,
    pool: ConnectionPool,
    process_config: ConfigBoot,
) -> None:
    columns = (
        "call_id, occurred_at, machine, cluster, billing, usage_kind, model, provider, calls, "
        "complete, tokens_in, tokens_out, cache_write_30m, cost_usd, price_snapshot, price_source"
    )
    for call_id, cost, source in (("unpriced", None, None), ("priced", Decimal("9"), "live")):
        db_conn.execute(
            f"INSERT INTO llm_usage_ledger ({columns}) VALUES "  # noqa: S608
            "(%s, %s, 'm', 'c', 'api', 'agent', 'gpt-6.1-sol', 'openai', 1, true, 10000, 100, "
            "3000, %s, '{}'::jsonb, %s)",
            (call_id, _LEDGER_AT, cost, source),
        )
    db_conn.commit()

    _pass(
        pool,
        database,
        process_config,
        LoopProgress("dispatch", timeout_s=60.0, clock=system_clock()),
    )

    rows = dict(db_conn.execute("SELECT call_id, cost_usd FROM llm_usage_ledger"))
    assert rows == {"unpriced": Decimal("0.0225"), "priced": Decimal("9")}


def test_loop_components_report_fresh_loops_as_ok() -> None:
    liveness = LivenessGroup(clock=system_clock())
    liveness.register("dispatch", timeout_s=15.0)
    liveness.register("registry_gauge", timeout_s=20.0)

    assert daemon.loop_components(
        liveness, {"dispatch": 15.0, "registry_gauge": 20.0}, now=time.time()
    ) == [
        {"name": "dispatch", "status": "ok", "progress": "idle"},
        {"name": "registry_gauge", "status": "ok", "progress": "idle"},
    ]


def test_loop_components_report_a_wedged_loop_and_why() -> None:
    liveness = LivenessGroup(clock=system_clock())
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
    liveness = LivenessGroup(clock=system_clock())
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

    configured = DaemonSettings.model_validate(
        {
            "MEIVA_EVENTS_MAINTENANCE_PASS_DEADLINE_S": "15.5",
        }
    )
    assert configured.events_maintenance_pass_deadline_s == 15.5


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


async def test_the_health_endpoint_lists_the_loops_the_unit_runs(
    db_conn: psycopg.Connection, pool_probe: PoolProbe, tmp_path: Path
) -> None:
    database = pool_probe.database()
    boot = _boot()
    port = _port(boot, tmp_path)
    service = _run(boot, database, tmp_path)
    try:
        await wait_serving(port, tmp_path / "run" / "events_maintenance.pid")
        status, body = await asyncio.to_thread(healthz, port)
    finally:
        service.cancel()
        await asyncio.gather(service, return_exceptions=True)

    assert status == 200
    assert component_names(body) == ["dispatch", "registry_gauge"]
    await assert_released(pool_probe, tmp_path / "run" / "events_maintenance.pid", port)


# meiva:note
# The loops share one TaskGroup: one that raises cancels the others and `run` leaves with the
# error, so the supervisor restarts the service.
@pytest.mark.parametrize(
    ("crashing", "table"),
    [
        ("dispatch", "audit_events_canonical"),
    ],
)
async def test_a_crashing_loop_cancels_its_siblings_and_ends_the_service(
    db_conn: psycopg.Connection,
    pool_probe: PoolProbe,
    tmp_path: Path,
    crashing: str,
    table: str,
) -> None:
    database = pool_probe.database()
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
    await assert_released(pool_probe, tmp_path / "run" / "events_maintenance.pid", port)


# meiva:note
# A worker thread blocked in the database past its deadline fails health, and stopping the
# service cancels its proxy instead of joining the thread.
async def test_stopping_the_service_does_not_wait_for_a_pass_past_its_deadline(
    db_conn: psycopg.Connection, pool_probe: PoolProbe, db_url: str, tmp_path: Path
) -> None:
    database = pool_probe.database()
    boot = _boot(events_maintenance_pass_deadline_s=0.5)
    port = _port(boot, tmp_path)
    pidfile = tmp_path / "run" / "events_maintenance.pid"
    before = asyncio.all_tasks()

    with table_locked(db_url, "audit_events"):
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
        assert pool_probe.owned(pool_probe.rows()), (
            "the blocked worker is still waiting on its lock"
        )

    await assert_released(pool_probe, pidfile, port)
