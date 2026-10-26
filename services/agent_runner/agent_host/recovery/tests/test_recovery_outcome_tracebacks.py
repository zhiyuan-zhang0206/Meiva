from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import psycopg
from psycopg import errors, sql
from psycopg_pool import AsyncConnectionPool

from agent.graph.llm_errors import FatalProviderError
from agent.ownership.hosted import admit_hosted_runtime
from agent.turn.runloop import handle_fatal_llm_error
from base.agents.context import MeivaContext
from base.agents.tests.units import spawn_agent
from base.clock import Clock
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import defaults
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.plugin_providers import build_model_catalog
from services.agent_runner.agent_host.tests.table_faults import refused_write


def _permanent_rejection() -> FatalProviderError:
    return FatalProviderError(
        "provider permanently rejected (HTTP 400): content risk",
        error_class="permanent",
        provider="deepseek",
        status=400,
    )


def _context(pool: AsyncConnectionPool, *, database_gate: ProcessDbGate) -> MeivaContext:
    return MeivaContext(
        ops_pool=pool,
        llm=MagicMock(),
        event_publisher=MagicMock(),
        agent=AgentSlices.resolve(default_reader=defaults()),
        db=Database.from_settings(gate=database_gate),
        bus=EventBus.from_settings(rng=seeded_rng()),
        catalog=build_model_catalog(),
        clock_factory=Clock.from_settings,
    )


async def _reject(
    pool: AsyncConnectionPool, agent_id: int, *, database_gate: ProcessDbGate
) -> None:
    await handle_fatal_llm_error(
        _permanent_rejection(),
        _context(pool, database_gate=database_gate),
        agent_id=agent_id,
        circuit=None,
        occurred_at=datetime(2026, 9, 16, 6, 0, tzinfo=UTC),
    )


def _sole_record(loguru_records: list[dict[str, Any]], fragment: str) -> dict[str, Any]:
    matches = [r for r in loguru_records if fragment in r["message"]]
    assert len(matches) == 1
    return matches[0]


async def test_streak_write_failure_keeps_its_traceback(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    db_conn: psycopg.Connection,
    loguru_records: list[dict[str, Any]],
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with refused_write(
        db_conn,
        table="agents_meta",
        event="UPDATE OF permanent_reject_streak",
        when=sql.SQL("NEW.permanent_reject_streak > OLD.permanent_reject_streak"),
    ):
        await _reject(aops_pool, agent_id, database_gate=database_gate)

    record = _sole_record(loguru_records, "failed to record a permanent-rejection streak")
    assert record["exception"] is not None
    assert record["exception"].type is errors.RaiseException


async def test_halt_suppression_failure_keeps_its_traceback(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    db_conn: psycopg.Connection,
    loguru_records: list[dict[str, Any]],
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent_id = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with refused_write(
        db_conn,
        table="agents_meta",
        event="UPDATE OF wake_suppressed_until",
        when=sql.SQL("NEW.wake_suppressed_until IS NOT NULL"),
    ):
        await _reject(aops_pool, agent_id, database_gate=database_gate)

    record = _sole_record(loguru_records, "failed to suppress automatic wakes")
    assert record["exception"] is not None
    assert record["exception"].type is errors.RaiseException


async def test_ancestor_report_failure_keeps_its_traceback(
    redis_url: str,
    aops_pool: AsyncConnectionPool,
    db_conn: psycopg.Connection,
    database: Database,
    loguru_records: list[dict[str, Any]],
    *,
    model_catalog: ModelCatalog,
    config_authority: ConfigAuthority,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    parent = spawn_agent(
        spawner="user",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    assert await admit_hosted_runtime(
        aops_pool, parent, machine_identity.name(), uuid4(), db=database
    )
    child = spawn_agent(
        spawner=f"agent:{parent}",
        catalog=model_catalog,
        authority=config_authority,
        database_gate=database_gate,
        machine=machine_identity,
    )
    with refused_write(
        db_conn,
        table="inbound_messages",
        event="INSERT",
        when=sql.SQL("NEW.agent_id = {parent} AND NEW.content LIKE 'Descendant agent%'").format(
            parent=sql.Literal(parent)
        ),
    ):
        await _reject(aops_pool, child, database_gate=database_gate)

    record = _sole_record(loguru_records, "failed to enqueue the recovery-halt report")
    assert record["exception"] is not None
    assert record["exception"].type is errors.RaiseException
