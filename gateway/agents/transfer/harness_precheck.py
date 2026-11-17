# meiva:note
# A destination probe checks the stored external harness and effective pins before source shutdown.
from __future__ import annotations

import asyncio
from collections.abc import Mapping

from fastapi import HTTPException
from psycopg_pool import ConnectionPool

from base.agents.model.harness_models import resolve_probed_harness_selection
from base.api_contracts.agent_contract import Harness, InvalidModelConfig
from base.db.rows import query_one
from gateway.agents.transfer.rows import AgentPinsRow
from gateway.http.config import EnsureHarnesses


async def validate_transfer_harness(
    ensure_harnesses: EnsureHarnesses,
    harness: Harness,
    to_machine: str,
    pins: Mapping[str, object],
) -> None:
    if harness is Harness.MEIVA:
        return
    rows = await ensure_harnesses(to_machine)
    row = next((item for item in rows if item.harness is harness), None)
    if row is None or not row.launchable:
        reason = row.reason if row is not None else "missing probe row"
        raise HTTPException(
            409, f"harness {harness.value!r} is not launchable on {to_machine!r}: {reason}"
        )
    try:
        resolve_probed_harness_selection(
            harness,
            row.to_probed_models(),
            requested_model=pins.get("llm_model"),
            requested_effort=pins.get("reasoning_effort"),
        )
    except InvalidModelConfig as exc:
        raise HTTPException(422, str(exc)) from exc


def _effective_pins_blocking(pool: ConnectionPool, agent_id: int) -> dict[str, object]:
    with pool.connection() as conn:
        row = query_one(
            conn,
            AgentPinsRow,
            "SELECT config_overlay, birth_config FROM agents_meta WHERE id = %s",
            (agent_id,),
        )
    if row is None:
        raise HTTPException(404, f"agent {agent_id} does not exist")
    return {**(row.birth_config or {}), **(row.config_overlay or {})}


async def validate_agent_transfer_harness(
    pool: ConnectionPool,
    ensure_harnesses: EnsureHarnesses,
    agent_id: int,
    harness: Harness,
    to_machine: str,
) -> None:
    if harness is Harness.MEIVA:
        return
    pins = await asyncio.to_thread(_effective_pins_blocking, pool, agent_id)
    await validate_transfer_harness(ensure_harnesses, harness, to_machine, pins)
