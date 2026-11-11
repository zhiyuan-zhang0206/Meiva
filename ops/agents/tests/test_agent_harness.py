from __future__ import annotations

from typing import cast

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
        config={"llm_model": "gpt-6.1-sol", "reasoning_effort": "high"},
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    assert _harness(db_conn, agent) == "codex"
    assert db_conn.execute(
        "SELECT birth_config->>'harness_model_selection' FROM agents_meta WHERE id=%s",
        (agent,),
    ).fetchone() == ("1",)


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


def test_fork_of_an_antigravity_source_is_refused(
    redis_url: str,
    db_conn: psycopg.Connection,
    *,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    machine_identity: MachineIdentityReader,
) -> None:
    source = spawn_agent(
        harness="antigravity",
        config={"llm_model": "gemini-4-argon", "reasoning_effort": "high"},
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    insert_checkpoint(db_conn, source, "ckpt-1")
    before = _birth_row_counts(db_conn)

    with pytest.raises(ForkHarnessMismatch, match="no supported fork entry"):
        spawn_agent(
            fork_from=source,
            fork_checkpoint="ckpt-1",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )

    assert _birth_row_counts(db_conn) == before


_EXTERNAL_MODELS = {"claude_code": "claude-sonnet-5-5", "codex": "gpt-6.1-sol"}


@pytest.mark.parametrize("external_harness", ["claude_code", "codex"])
def test_fork_of_a_same_machine_external_source_keeps_its_harness_and_copies_its_timeline(
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
        config={"llm_model": _EXTERNAL_MODELS[external_harness], "reasoning_effort": "high"},
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )
    insert_checkpoint(db_conn, source, "ckpt-1")
    with db_conn.cursor() as cur:
        for seq in range(3):
            cur.execute(
                "INSERT INTO agent_harness_messages (agent_id, seq, harness, item, inbound_id) "
                "VALUES (%s, %s, %s, %s::jsonb, %s)",
                (source, seq, external_harness, f'{{"id": "{seq}.0"}}', 100 + seq),
            )
    db_conn.commit()

    fork = spawn_agent(
        fork_from=source,
        fork_checkpoint="ckpt-1",
        config_authority=config_authority,
        model_catalog=model_catalog,
        database_gate=database_gate,
        local_machine=machine_identity,
    )

    assert _harness(db_conn, fork) == external_harness
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT seq, harness, item->>'id', inbound_id FROM agent_harness_messages "
            "WHERE agent_id = %s ORDER BY seq",
            (fork,),
        )
        assert cur.fetchall() == [(seq, external_harness, f"{seq}.0", None) for seq in range(3)]
        cur.execute("SELECT count(*) FROM agent_publications WHERE agent_id = %s", (fork,))
        assert cur.fetchone() == (0,)
        cur.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", (str(fork),))
        assert cur.fetchone() == (0,)
        cur.execute("SELECT kind, source FROM inbound_messages WHERE agent_id = %s", (fork,))
        assert cur.fetchall() == [("fork", f"agent:{source}")]


def test_fork_onto_another_machine_than_its_external_source_is_refused() -> None:
    from base.agents.birth.harness import resolve_birth_harness
    from base.api_contracts.agent_contract import Harness

    class _Cursor:
        def execute(self, query: str, params: tuple[object, ...]) -> None:
            self.query = query

        def fetchone(self) -> tuple[object, ...]:
            return (Harness.CODEX.value, "macmini")

    cursor = cast("psycopg.Cursor[tuple[object, ...]]", _Cursor())
    with pytest.raises(ForkHarnessMismatch, match="local file there"):
        resolve_birth_harness(cursor, None, 5, machine="ubuntu")
    assert resolve_birth_harness(cursor, None, 5, machine="macmini") == "codex"


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
        config={"llm_model": "gpt-6.1-sol", "reasoning_effort": "high"},
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
    # meiva:link base/agents/docs/harness/harness.md
    assert _birth_row_counts(db_conn) == before
