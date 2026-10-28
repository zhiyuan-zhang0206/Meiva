# meiva:note
# Runner-side spawn validation and repeatable dispatcher wake.

from __future__ import annotations

import asyncio

from psycopg_pool import ConnectionPool

from base.agents.birth.harness import LAUNCHABLE_HARNESSES, resolve_birth_harness
from base.agents.birth.spawn import latest_checkpoint_id
from base.agents.impersonation.wake import publish_inbound_wake
from base.agents.model.birth_config import uses_harness_model_selection
from base.api_contracts.agent_contract import ForkSourceEmpty, Harness
from base.config.agent_pins import resolve_agent_config_pins
from base.config.service_read import ConfigAuthority
from base.db import Database
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.factory import process_provider_keys, validate_model_launch
from ops.lifecycle.launch_row import StoredLaunch, require_current_launch_row
from ops.rpc_schemas import LaunchAgentRequest, SpawnAgentRequest, SpawnedAgent


# meiva:note
# Validate a gateway-created row and wake its host without changing identity.
#
# The gateway supplies the committed attempt and already-stored inbound. No failure here
# terminates the row: the gateway records launch failure and the pending scan can still deliver
# the committed first prompt.
async def launch_agent_op(
    db: Database,
    bus: EventBus,
    body: LaunchAgentRequest,
    db_pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
    authority: ConfigAuthority,
    local_machine: str,
) -> SpawnedAgent:
    harness = await asyncio.to_thread(
        validate_launch_row, db_pool, body, local_machine=local_machine
    )
    if harness is Harness.MEIVA or uses_harness_model_selection(body.birth_config):
        keys = await asyncio.to_thread(process_provider_keys, catalog)
        validate_model_launch(
            catalog=catalog,
            keys=keys,
            llm_override=authority.service_field_value("llm_override"),
            model=authority.service_field_value("llm_model"),
            config=resolve_agent_config_pins(body.config, body.birth_config),
            harness=harness,
        )
    publish_inbound_wake(db, bus, body.agent_id, "0")
    return SpawnedAgent(id=body.agent_id)


# meiva:note
# Read the agent's row and refuse a stale, misplaced or unlaunchable attempt.
def validate_launch_row(
    db_pool: ConnectionPool, body: LaunchAgentRequest, *, local_machine: str
) -> Harness:
    with db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT machine, last_launch_attempt_id, status, harness FROM agents_meta WHERE id=%s",
            (body.agent_id,),
        )
        row = cur.fetchone()
    return require_current_launch_row(
        None if row is None else StoredLaunch(*row),
        agent_id=body.agent_id,
        launch_attempt_id=body.launch_attempt_id,
        local_machine=local_machine,
        launchable=LAUNCHABLE_HARNESSES,
    )


def spawn_prechecks_blocking(
    body: SpawnAgentRequest,
    db_pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
    keys: ProviderKeys,
    authority: ConfigAuthority,
) -> str | None:
    """Sync spawn pre-checks — via to_thread: model-config validation (may read
    provider API keys) + fork checkpoint lookup. Returns the fork checkpoint
    (None for a plain spawn)."""
    from base.agents.model.model_config import validate_spawn_model_config

    with db_pool.connection() as conn, conn.cursor() as cur:
        resolve_birth_harness(cur, body.harness, body.fork_from)
        validate_spawn_model_config(
            cur,
            body.config,
            body.fork_from,
            catalog=catalog,
            keys=keys,
            authority=authority,
            llm_override=authority.service_field_value("llm_override"),
            harness=body.harness or Harness.MEIVA,
        )
    fork_checkpoint: str | None = None
    if body.fork_from is not None:
        with db_pool.connection() as conn, conn.cursor() as cur:
            fork_checkpoint = latest_checkpoint_id(cur, body.fork_from)
        if fork_checkpoint is None:
            raise ForkSourceEmpty(
                f"agent {body.fork_from} has no checkpoint — it may not have run any LLM/exec step yet"
            )
    if body.prompt is not None and body.prompt_source is None:
        raise RuntimeError("prompt_source missing despite schema validator")
    return fork_checkpoint
