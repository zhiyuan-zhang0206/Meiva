from __future__ import annotations

import psycopg
from psycopg_pool import AsyncConnectionPool

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.config.tests.config_authority import config_authority as config_authority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import model_catalog as model_catalog
from ops.agents.tests.test_agents_internals import insert_checkpoint, spawn_agent
from services.agent_runner.agent_host.scheduling.wake_screening import read_stored_config


async def test_the_runner_reads_the_fork_source_with_the_stored_config(
    redis_url: str,
    db_conn: psycopg.Connection,
    aops_pool: AsyncConnectionPool,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    source = spawn_agent(
        harness="codex",
        config={"llm_model": "gpt-6.1-sol", "reasoning_effort": "high"},
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    insert_checkpoint(db_conn, source, "ckpt-1")
    fork = spawn_agent(
        fork_from=source,
        fork_checkpoint="ckpt-1",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )

    stored = await read_stored_config(aops_pool, fork)
    assert stored is not None
    assert stored.fork_source_agent_id == source
    plain = await read_stored_config(aops_pool, source)
    assert plain is not None and plain.fork_source_agent_id is None
