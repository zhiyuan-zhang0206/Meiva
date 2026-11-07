"""Spawn stamping — `base.agents.birth.spawn.create_agent_row` freezes the frozen set on the row.

The agents_meta INSERT is THE spawn boundary (every spawn in the system funnels
through it), so these tests go through the create+launch split (`create_agent_row`
+ the launch) rather than through `resolve_birth_config` directly: what matters
is that the stamp lands on the row and rides out to the launched child, not just
that the resolver can compute it.

The autouse `_guard_agent_launch` fixture replaces the real launch with a recorder,
so `launched_agents` is what the child would have been handed.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from base.agents.birth.spawn import create_agent_row
from base.clock import SYSTEM_TIMERS
from base.clock.tests.seeded_rng import seeded_rng
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import frozen_field_names
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog


def _spawn_agent(
    *,
    spawner: str = "user",
    config: dict[str, object] | None = None,
    config_authority: ConfigAuthority,
    model_catalog: ModelCatalog,
    database_gate: ProcessDbGate,
    local_machine: MachineIdentityReader,
    **kw: Any,
) -> int:
    """Test setup helper — the #1236 split collapsed for setup: create_agent_row
    (gateway-side, the main data-plane identity) + `_launch_agent_process`
    (runner-side; the autouse guard spy records the launch so tests can assert
    the stamp rides out to the child)."""

    if config is None and kw.get("fork_from") is None and not spawner.startswith("agent:"):
        config = {"llm_model": "deepseek-flash"}
    agent_id, _birth_config, _prompt_id, _attempt_id = create_agent_row(
        Database.from_settings(gate=database_gate),
        EventBus.from_settings(rng=seeded_rng(), timers=SYSTEM_TIMERS),
        spawner=spawner,
        machine=local_machine.name(),
        config=config,
        **kw,
        catalog=model_catalog,
        authority=config_authority,
    )
    return agent_id


@pytest.fixture(autouse=True)
def _unset(cluster_defaults_unset: None) -> None:
    """Every test here starts from "no cluster choice" (see the shared fixture)."""


def _birth_config(conn: psycopg.Connection, agent_id: int) -> dict[str, object] | None:
    with conn.cursor() as cur:
        cur.execute("SELECT birth_config FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    assert row is not None
    return row[0]


def _first_checkpoint(conn: psycopg.Connection, agent_id: int) -> str:
    """Give `agent_id` one checkpoint so it can be forked from."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, "
            "checkpoint, metadata) VALUES (%s, '', 'ckpt-1', 'x', '{}'::jsonb, '{}'::jsonb)",
            (str(agent_id),),
        )
    conn.commit()
    return "ckpt-1"


class TestSpawnStamping:
    def test_absent_from_overlay_frozen_fields_are_stamped(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        parent_id = _spawn_agent(
            spawner="test",
            config={"llm_model": "deepseek-flash"},
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        child_id = _spawn_agent(
            spawner=f"agent:{parent_id}",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert set(_birth_config(db_conn, child_id) or {}) == frozen_field_names()

    def test_overlay_present_frozen_field_is_not_stamped(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        agent_id = _spawn_agent(
            spawner="test",
            config={"llm_model": "claude-sonnet-5"},
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        stamped = _birth_config(db_conn, agent_id) or {}
        assert "llm_model" not in stamped
        assert set(stamped) == frozen_field_names() - {"llm_model"}

    def test_the_parent_model_is_what_gets_frozen(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        parent_id = _spawn_agent(
            spawner="test",
            config={"llm_model": "claude-sonnet-5"},
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        child_id = _spawn_agent(
            spawner=f"agent:{parent_id}",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert (_birth_config(db_conn, child_id) or {})["llm_model"] == "claude-sonnet-5"

    def test_root_spawn_without_model_fails(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        with pytest.raises(ValueError, match="llm_model must be explicitly provided"):
            _spawn_agent(
                spawner="test",
                config={},
                config_authority=config_authority,
                model_catalog=model_catalog,
                database_gate=database_gate,
                local_machine=machine_identity,
            )


class TestReplayOnWake:
    """Resurrection preserves the birth configuration for hosted successor admission."""

    def test_resurrect_replays_the_stamp(
        self,
        db_conn: psycopg.Connection,
        database: Database,
        event_bus: EventBus,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        from ops.agents.wake import resurrect_agent

        agent_id = _spawn_agent(
            spawner="test",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        stamp = _birth_config(db_conn, agent_id)
        with db_conn.cursor() as cur:
            cur.execute(
                "UPDATE agents_meta SET status = 'terminated', termination_source = 'exit', "
                "runtime_generation=gen_random_uuid(), "
                "runtime_owner=gen_random_uuid() "
                "WHERE id = %s",
                (agent_id,),
            )
        db_conn.commit()
        resurrect_agent(
            database,
            event_bus,
            agent_id,
            resurrected_by="user",
        )
        assert _birth_config(db_conn, agent_id) == stamp


class TestForkInheritance:
    def test_a_fork_inherits_the_parents_stamp(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        parent = _spawn_agent(
            spawner="test",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        ckpt = _first_checkpoint(db_conn, parent)
        child = _spawn_agent(
            spawner="test",
            fork_from=parent,
            fork_checkpoint=ckpt,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert _birth_config(db_conn, child) == _birth_config(db_conn, parent)

    def test_a_fork_of_an_unstamped_agent_is_stamped_fresh(
        self,
        redis_url: str,
        db_conn: psycopg.Connection,
        *,
        config_authority: ConfigAuthority,
        model_catalog: ModelCatalog,
        database_gate: ProcessDbGate,
        machine_identity: MachineIdentityReader,
    ) -> None:
        """A pre-column agent (or one the migration backfill skipped) has NULL. Its
        fork must still come out fully stamped rather than inherit the hole."""
        parent = _spawn_agent(
            spawner="test",
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        ckpt = _first_checkpoint(db_conn, parent)
        with db_conn.cursor() as cur:
            cur.execute("UPDATE agents_meta SET birth_config = NULL WHERE id = %s", (parent,))
        db_conn.commit()
        child = _spawn_agent(
            spawner="test",
            fork_from=parent,
            fork_checkpoint=ckpt,
            config_authority=config_authority,
            model_catalog=model_catalog,
            database_gate=database_gate,
            local_machine=machine_identity,
        )
        assert set(_birth_config(db_conn, child) or {}) == frozen_field_names() - {"llm_model"}
