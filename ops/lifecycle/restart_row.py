# meiva:note
# The agent status, harness and configuration rows read or updated by restart transitions.

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from base.agents.epochs.model import RecordExtentReader
from base.agents.epochs.write import (
    Statement,
    SwitchFacts,
    harness_switch,
    merge_overlay,
    store_harness,
    switched_by_of,
)
from base.agents.model.overlay_columns import split_overlay_columns
from base.api_contracts.agent_contract import AgentNotFound
from base.db.rows import query_one
from base.db.transaction import write_transaction
from base.lm.catalog import ModelCatalog
from base.lm.registry import normalize_overlay_llm_model
from ops.rpc_schemas import RestartAgentRequest

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
    body: RestartAgentRequest,
    catalog: ModelCatalog,
    read_extent: RecordExtentReader,
) -> None:
    overlay = split_overlay_columns(body.config_overlay).fields
    with write_transaction(db_pool) as conn:
        row = query_one(
            conn,
            _StatusAndHarness,
            "SELECT status, harness FROM agents_meta WHERE id = %s FOR UPDATE",
            (agent_id,),
        )
        if row is None:
            raise AgentNotFound(f"agent {agent_id} does not exist")
        stored_harness = row.harness
        if overlay and target_harness == "meiva":
            normalize_overlay_llm_model(overlay, models=catalog.models)
        statements: list[Statement] = [merge_overlay(agent_id, overlay)] if overlay else []
        if stored_harness == target_harness:
            statements.extend(store_harness(agent_id, target_harness))
        else:
            model: object = overlay.get("llm_model")
            statements.extend(
                harness_switch(
                    agent_id,
                    SwitchFacts(
                        new_harness=target_harness,
                        model=model if isinstance(model, str) else None,
                        by=switched_by_of(body.source, agent_id),
                        message=body.message,
                        leaving=read_extent(agent_id, stored_harness),
                        arriving=read_extent(agent_id, target_harness),
                    ),
                )
            )
        for statement, params in statements:
            conn.execute(statement, params)
