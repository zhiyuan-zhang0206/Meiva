"""Composer commands endpoint — `GET /api/commands`.

Lists the registered prompt templates (see `base.packages.composer_commands`) for the web
Composer's `/`-autocomplete. Read-only filesystem scan; no auth beyond the
gateway's default session gate. Only the metadata the dropdown needs is
returned — the body is never sent to the browser, since expansion happens
server-side in the agent's claim node (`base.packages.composer_commands.expand_command`).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from gateway.schemas import CommandItem
from ops.cluster import rpc as cluster_rpc

router = APIRouter()
_log = logging.getLogger(__name__)
_AGENT_SKILL_VIEW_TIMEOUT_S = 3.0


_HARNESS_FORBIDDEN_COMMANDS: frozenset[str] = frozenset({"compact"})


def _local_commands(request: Request, *, harness: str = "meiva") -> list[CommandItem]:
    """The gateway-local catalog: the unscoped view and the availability fallback."""
    discovered = request.app.state.command_catalog.discover()
    return [
        CommandItem(
            name=c["name"],
            description=c["description"],
            instruction_hint=c["instruction_hint"],
        )
        for c in discovered
        if harness == "meiva" or c["name"] not in _HARNESS_FORBIDDEN_COMMANDS
    ]


# meiva:note
# (machine, harness) recorded for an agent, or (None, 'meiva') when the row is absent.
def _agent_machine_and_harness(request: Request, agent_id: int) -> tuple[str | None, str]:
    with request.app.state.db_pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine, harness FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        return None, "meiva"
    machine = str(row[0]) if row[0] is not None else None
    harness = str(row[1]) if len(row) > 1 and row[1] is not None else "meiva"
    return machine, harness


@router.get("/api/commands")
async def get_commands(request: Request, agent_id: int | None = None) -> list[CommandItem]:
    """Composer commands, globally by default or as one agent sees them.

    An agent view always dials the machine recorded on ``agents_meta`` (including
    the gateway's own machine).  A missing/offline/version-skewed runner falls
    back to the historical gateway-local catalog so autocomplete remains usable.
    """
    if agent_id is None:
        return _local_commands(request)

    machine, harness = await asyncio.to_thread(_agent_machine_and_harness, request, agent_id)
    if machine is None:
        _log.warning("commands: agent %s has no agents_meta row; using local fallback", agent_id)
        return _local_commands(request, harness=harness)
    try:
        result = await cluster_rpc.dispatch_to_machine(
            request.app.state.db,
            machine,
            "agent_skill_view",
            {"agent_id": agent_id},
            timeout_s=_AGENT_SKILL_VIEW_TIMEOUT_S,
        )
    except (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed) as exc:
        _log.warning(
            "commands: agent %s command view unavailable on %s; using local fallback: %s",
            agent_id,
            machine,
            exc,
        )
        return _local_commands(request, harness=harness)
    return [
        CommandItem.model_validate(command)
        for command in result["commands"]
        if harness == "meiva" or command.get("name") not in _HARNESS_FORBIDDEN_COMMANDS
    ]
