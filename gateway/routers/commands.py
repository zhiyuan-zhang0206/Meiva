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
from dataclasses import dataclass

from fastapi import APIRouter, Request

from base.db.rows import query_one
from base.host.boundary.json_values import is_object_dict, is_object_list
from gateway.http.config import gateway_rpc, gateway_state
from gateway.schemas import CommandItem
from ops.cluster import rpc as cluster_rpc

router = APIRouter()
_log = logging.getLogger(__name__)
_AGENT_SKILL_VIEW_TIMEOUT_S = 3.0


_HARNESS_FORBIDDEN_COMMANDS: frozenset[str] = frozenset({"compact"})


def _local_commands(request: Request, *, harness: str = "meiva") -> list[CommandItem]:
    """The gateway-local catalog: the unscoped view and the availability fallback."""
    discovered = gateway_state(request).command_catalog.discover()
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
# The machine and harness columns of an agent; either is NULL for a row that never recorded it.
@dataclass(frozen=True)
class _MachineHarnessRow:
    machine: str | None
    harness: str | None


# meiva:note
# The command entries of an `agent_skill_view` answer, each still a decoded JSON object.
def _command_entries(value: object) -> list[dict[str, object]]:
    if not is_object_list(value):
        raise TypeError("agent_skill_view answered a non-list `commands`")
    entries: list[dict[str, object]] = []
    for entry in value:
        if not is_object_dict(entry):
            raise TypeError("agent_skill_view answered a non-object command")
        entries.append(entry)
    return entries


# meiva:note
# (machine, harness) recorded for an agent, or (None, 'meiva') when the row is absent.
def _agent_machine_and_harness(request: Request, agent_id: int) -> tuple[str | None, str]:
    with gateway_state(request).db_pool.connection() as conn:
        row = query_one(
            conn,
            _MachineHarnessRow,
            "SELECT machine, harness FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
    if row is None:
        return None, "meiva"
    return row.machine, row.harness if row.harness is not None else "meiva"


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
        result = await gateway_rpc(request).dispatch_to_machine(
            gateway_state(request).db,
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
        for command in _command_entries(result.get("commands"))
        if harness == "meiva" or command.get("name") not in _HARNESS_FORBIDDEN_COMMANDS
    ]
