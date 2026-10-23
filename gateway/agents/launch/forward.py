# meiva:note
# Cross-machine forward helpers for the /api/agents/* router surface.
#
# Lifecycle operations (resurrect / force-terminate / restart) and spawn must
# run on the agent's home machine via that machine's ops server — the gateway is
# a pure router and never shortcuts a co-located runner. These helpers are
# endpoint-private routing effects, shared by `router.py` (spawn),
# `lifecycle.py` (lifecycle ops) and the op-forwarding routers
# (guide / packages / schedules), so they live in their own module — the ops
# server never sees them (forwarding never recurses inside an op, see
# ops/lifecycle/__init__.py).
#
# Every decision here (the op to send, the error an answer stands for) is a pure
# function in `gateway.agents.launch.launch_plan` / `lifecycle_plan`; this module only performs the registry
# read, the dispatch and the clock bound around it. A test serves a runner's answer
# from a real listener (`gateway/tests/support/runner_ops`).

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger
from psycopg_pool import ConnectionPool

from base.api_contracts.agent_contract import AgentNotFound
from base.db import Database
from gateway.agents.launch import launch_plan, lifecycle_plan
from gateway.http.config import GatewayConfig
from ops.cluster import rpc as cluster_rpc
from ops.rpc_schemas import LaunchAgentRequest, SpawnedAgent


# meiva:note
# Lifecycle operations (resurrect / force-terminate / etc.) must run on
# the agent's home machine (`agents_meta.machine`) — they mutate physical
# host state (session / OS process); not doing them on the home
# machine is a noop / starts on the wrong host.
#
# Always POSTs a 'lifecycle' op to the target machine's ops server and
# blocks on the response, then returns the result dict (caller reshapes to
# the business model). No local shortcut — even when target IS the local
# machine, the ops server on localhost executes the op in-process. Single
# box is just the special case where the ops server is reachable at
# 127.0.0.1.
#
# The target machine's meiva-ops server executes the op in-process via
# `ops.lifecycle.lifecycle_op` — no cross-machine forwarding inside the
# op, no risk of a forwarding loop.
#
# Raises:
#     AgentNotFound: agent_id does not exist (no agents_meta row).
#     CrossMachineGatewayUnavailable: the target machine's ops server was
#         unreachable (offline / not yet registered / timed out).
#     Other MeivaAgentError subclasses: business errors raised by the
#         target machine's op are passed through.
async def forward_to_home_machine(
    agent_id: int,
    path: str,
    json_body: dict[str, Any],
    *,
    db: Database,
    pool: ConnectionPool,
    config: GatewayConfig,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    target = await asyncio.to_thread(_home_machine_blocking, pool, agent_id)
    return await enqueue_lifecycle(
        db, target, path, json_body, config=config, idempotency_key=idempotency_key
    )


# meiva:note
# POST a 'lifecycle' op to the target machine's ops server, return its result.
#
# Browser clients and the agent UI give lifecycle requests a short response window,
# so an offline runner must become a clear 502 before that window expires: the
# config bounds one attempt, the retries and the whole wait (the generic cluster-RPC
# policy is more patient, for rollout work). The one retry keeps the server-side
# idempotency envelope across a transient connection drop.
#
# Translates a 'failed' outcome (target machine's op raised an MeivaAgentError)
# into the wire-protocol exception so the gateway's handler re-emits the
# original semantics.
async def enqueue_lifecycle(
    db: Database,
    target: str,
    path: str,
    json_body: dict[str, Any],
    *,
    config: GatewayConfig,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    op = lifecycle_plan.lifecycle_op(
        path,
        json_body,
        timeout_s=config.lifecycle_attempt_timeout_seconds,
        retries=config.lifecycle_retries,
        idempotency_key=idempotency_key,
    )
    try:
        async with asyncio.timeout(config.lifecycle_deadline_seconds):
            return await cluster_rpc.dispatch_to_machine(
                db,
                target_machine=target,
                kind=op.kind,
                payload=op.payload,
                timeout_s=op.timeout_s,
                retries=op.retries,
                idempotency_key=op.idempotency_key,
            )
    except TimeoutError as exc:
        raise lifecycle_plan.lifecycle_timeout(target, config.lifecycle_deadline_seconds) from exc
    except cluster_rpc.ClusterOpUnreachable as exc:
        raise lifecycle_plan.lifecycle_unreachable(target, str(exc)) from exc
    except cluster_rpc.ClusterOpFailed as exc:
        failure = lifecycle_plan.proxied_failure(exc.result)
        if failure.unknown_reason is not None:
            logger.warning(
                "Unknown error reason '{}' in cross-machine payload", failure.unknown_reason
            )
        raise failure.error from exc


# meiva:note
# POST a launch op to the target machine's ops server.
#
# The target machine's meiva-ops server dispatches in-process to
# `ops.lifecycle.launch_agent_op`. The gateway already committed the
# agent row and first prompt; the runner validates the attempt, wakes the
# hosted agent, and returns the SpawnedAgent dict.
#
# Raises:
#     LaunchForwardError: launch_unreachable when the ops server cannot be
#         reached or returns a malformed result; launch_rejected when the
#         target op rejects the launch. The target's business reason is
#         included in the error detail.
async def forward_spawn_to_remote(
    db: Database, target: str, body: LaunchAgentRequest
) -> SpawnedAgent:
    try:
        result = await cluster_rpc.dispatch_to_machine(
            db,
            target_machine=target,
            kind="spawn-launch",
            payload=launch_plan.launch_op_payload(body),
        )
    except cluster_rpc.ClusterOpUnreachable as exc:
        raise launch_plan.launch_unreachable(target, str(exc)) from exc
    except cluster_rpc.ClusterOpFailed as exc:
        raise launch_plan.launch_rejection(target, exc.result) from exc
    receipt = launch_plan.launch_receipt(target, result, body.agent_id)
    if isinstance(receipt, launch_plan.LaunchForwardError):
        raise receipt
    return receipt


# meiva:note
# Sync home-machine lookup for lifecycle ops — via to_thread.
def _home_machine_blocking(pool: ConnectionPool, agent_id: int) -> str:
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT machine FROM agents_meta WHERE id = %s", (agent_id,))
        row = cur.fetchone()
    if row is None:
        raise AgentNotFound(f"agent {agent_id} does not exist")
    return row[0]
