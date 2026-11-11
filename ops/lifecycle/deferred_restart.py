# meiva:note
# A restart the agent asked for itself, recorded now and applied by its host when the running turn
# ends. The write is one pending `restart` command (source `self`) carrying the transition
# (`base.agents.incarnation.lifecycle_commands.RestartTransition`): the target harness and the
# overlay to merge. Nothing else changes now, so a harness switch never touches an agent that is
# still working: the claim takes the command after the harness turn or native execution returns and
# the host's apply stores the harness and releases the incarnation in one transaction
# (`agent.ownership.hosted.apply_hosted_lifecycle`). At most one such command is open at a time:
# a second request while one waits is refused and names the first, so an agent that retries a call
# whose acknowledgement it did not see never queues two switches.
# meiva:link agent/graph/docs/turn-interrupts.md

from __future__ import annotations

import logging
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from base.agents.inbox.insert import insert_inbound_message
from base.agents.incarnation.restart_transition import RestartTransition
from base.agents.model.overlay_columns import split_overlay_columns
from base.db import Database
from base.db.rows import IdRow, query_one
from base.db.transaction import write_transaction
from base.events.live.bus import EventBus
from base.lm.catalog import ModelCatalog
from base.lm.registry import normalize_overlay_llm_model
from ops.rpc_schemas import RestartAgentRequest

_log = logging.getLogger(__name__)

SELF_SOURCE = "self"


# meiva:note
# The lifecycle status and stored harness of the agent's row.
@dataclass(frozen=True)
class _AgentRow:
    status: str
    harness: str


# meiva:note
# Record the agent's restart for its turn boundary and return the command's inbound id.
#
#     The gateway validated the request (target harness launchable on the machine, an explicit
#     model when switching, the overlay, the model) before forwarding it; this writes under the
#     agent row's lock and re-checks only what a race can change: the agent is alive and no
#     earlier restart of its own is still open. A withdrawn model of a native target is settled
#     here with the catalog, as the immediate restart settles it.
def defer_restart_blocking(
    db: Database,
    bus: EventBus,
    agent_id: int,
    body: RestartAgentRequest,
    db_pool: ConnectionPool,
    *,
    catalog: ModelCatalog,
) -> int:
    requested, overlay = split_overlay_columns(body.config_overlay)
    with write_transaction(db_pool) as conn:
        row = query_one(
            conn,
            _AgentRow,
            "SELECT status, harness FROM agents_meta WHERE id=%s FOR UPDATE",
            (agent_id,),
        )
        if row is None:
            raise RuntimeError(f"agent {agent_id} does not exist")
        if row.status not in ("running", "idling"):
            raise ValueError(
                f"cannot queue a restart for agent {agent_id} in status {row.status!r}"
            )
        target = row.harness if requested is None else requested.value
        if target == "meiva":
            receipt = normalize_overlay_llm_model(overlay, models=catalog.models)
            if receipt is not None:
                _log.warning(
                    "restart overlay llm_model %r is withdrawn; storing the "
                    "registered fallback %r (task #4306)",
                    receipt[0],
                    receipt[1],
                )
        open_row = query_one(
            conn,
            IdRow,
            "SELECT id FROM inbound_messages WHERE agent_id=%s AND kind='restart' "
            "AND source=%s AND status IN ('pending','claimed') ORDER BY id LIMIT 1",
            (agent_id, SELF_SOURCE),
        )
        if open_row is not None:
            raise ValueError(
                f"restart command {open_row.id} of agent {agent_id} is already waiting for the "
                "turn to end; finish the turn instead of requesting another"
            )
        return insert_inbound_message(
            conn,
            agent_id,
            body.message,
            source=SELF_SOURCE,
            kind="restart",
            payload=RestartTransition(target, overlay).payload(),
            database=db,
            bus=bus,
        )
