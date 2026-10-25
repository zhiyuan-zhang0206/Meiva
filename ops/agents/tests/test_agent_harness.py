from __future__ import annotations

import psycopg
import pytest
from psycopg import sql

from base.api_contracts.agent_contract import ForkHarnessMismatch
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.service_read import ConfigAuthority
from base.db.code_version_gate import ProcessDbGate
from base.lm.catalog import ModelCatalog
from ops.agents.tests.test_agents_internals import insert_checkpoint, spawn_agent


def _birth_row_counts(db: psycopg.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    with db.cursor() as cur:
        for table in ("agents", "agents_meta", "inbound_messages", "checkpoints"):
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
            row = cur.fetchone()
            assert row is not None
            counts[table] = row[0]
    return counts


def _harness(db: psycopg.Connection, agent_id: int) -> str:
    with db.cursor() as cur:
        cur.execute("SELECT harness FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def test_plain_spawn_defaults_to_the_builtin_runtime(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent = spawn_agent(
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    assert _harness(db_conn, agent) == "meiva"


def test_plain_spawn_records_the_requested_harness(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    agent = spawn_agent(
        harness="codex",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    assert _harness(db_conn, agent) == "codex"


def test_fork_inherits_the_source_harness(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    source = spawn_agent(
        harness="meiva",
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
    assert _harness(db_conn, fork) == "meiva"


def test_fork_naming_the_same_harness_is_accepted(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    source = spawn_agent(
        harness="meiva",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    insert_checkpoint(db_conn, source, "ckpt-1")

    fork = spawn_agent(
        fork_from=source,
        fork_checkpoint="ckpt-1",
        harness="meiva",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    assert _harness(db_conn, fork) == "meiva"


@pytest.mark.parametrize("external_harness", ["claude_code", "codex", "antigravity"])
def test_fork_of_an_external_harness_source_is_refused(
    external_harness: str,
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    source = spawn_agent(
        harness=external_harness,
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    insert_checkpoint(db_conn, source, "ckpt-1")
    before = _birth_row_counts(db_conn)

    with pytest.raises(ForkHarnessMismatch, match="only supported on the native 'meiva' harness"):
        spawn_agent(
            fork_from=source,
            fork_checkpoint="ckpt-1",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

    assert _birth_row_counts(db_conn) == before


def test_fork_naming_another_harness_is_refused(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    source = spawn_agent(
        harness="codex",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    insert_checkpoint(db_conn, source, "ckpt-1")
    before = _birth_row_counts(db_conn)

    with pytest.raises(ForkHarnessMismatch) as refusal:
        spawn_agent(
            fork_from=source,
            fork_checkpoint="ckpt-1",
            harness="meiva",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

    assert "immutable" in str(refusal.value)
    # meiva:note
    # Nothing of the refused birth survives: no identity row, no metadata row, no
    # copied conversation and no fork prompt, because the raise rolls the whole
    # birth transaction back.
    # meiva:link base/agents/docs/harness/harness.meiva.okf.md
    assert _birth_row_counts(db_conn) == before
