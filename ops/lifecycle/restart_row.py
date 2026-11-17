# meiva:note
# The agent status, harness and configuration rows read or updated by restart transitions.

from __future__ import annotations

import json
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from base.agents.model.overlay_columns import split_overlay_columns
from base.api_contracts.agent_contract import AgentNotFound
from base.db.rows import query_one
from base.db.transaction import write_transaction
from base.lm.catalog import ModelCatalog
from base.lm.registry import normalize_overlay_llm_model

__all__ = ["read_restart_status_and_harness_blocking", "update_harness_transition_meta_blocking"]


# meiva:note
# The lifecycle status and harness columns of an `agents_meta` row.
@dataclass(frozen=True)
class _StatusAndHarness:
    status: str
    harness: str


# meiva:note
# The agent's status and harness, as plain strings; an absent agent is `AgentNotFound`.
def read_restart_status_and_harness_blocking(
    db_pool: ConnectionPool, agent_id: int
) -> tuple[str, str]:
    with db_pool.connection() as conn:
        row = query_one(
            conn,
            _StatusAndHarness,
            "SELECT status, harness FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row.status, row.harness


def update_harness_transition_meta_blocking(
    db_pool: ConnectionPool,
    agent_id: int,
    target_harness: str,
    config_overlay: dict[str, object] | None,
    catalog: ModelCatalog,
) -> None:
    overlay_without_harness = split_overlay_columns(config_overlay).fields
    with write_transaction(db_pool) as conn:
        if overlay_without_harness:
            if target_harness == "meiva":
                normalize_overlay_llm_model(overlay_without_harness, models=catalog.models)
            conn.execute(
                "UPDATE agents_meta SET harness = %s, harness_configured_at = clock_timestamp(), "
                "config_overlay = COALESCE(config_overlay, '{}'::jsonb) || %s::jsonb WHERE id = %s",
                (target_harness, json.dumps(overlay_without_harness), agent_id),
            )
        else:
            conn.execute(
                "UPDATE agents_meta SET harness = %s, harness_configured_at = clock_timestamp() "
                "WHERE id = %s",
                (target_harness, agent_id),
            )
        if target_harness != "meiva":
            conn.execute(
                "UPDATE agents_meta SET birth_config = COALESCE(birth_config, '{}'::jsonb) || '{\"harness_model_selection\": 1}'::jsonb WHERE id = %s",
                (agent_id,),
            )
