"""Cross-machine plugin + MCP enable/disable panel — /api/inventory GET/PUT.

Inventory (plugin + MCP enable state) is an AGENT-HOST-ONLY surface: a
gateway runs no agent process, so it loads no plugins and connects to no
MCP servers, and must never appear here — not as an aggregate column, not as a
`?machine=` target. Every read/write is POSTed to a remote agent-runner's
ops server (which executes it in-process).

`?machine=` selects the host viewed/written:
- GET without `machine` -> the cross-machine AGGREGATE: fan out an inventory_read
  to every registered agent-runner concurrently, collapse into a per-item matrix;
  a host that times out / fails lands in `unreachable`, not in any item's cells.
  A host already known down — intentionally stopped, or inside the roster
  probe's failure backoff — is never dialed: it reports straight into
  `unreachable` (a dial could only hang; task #4127).
- GET with `machine` -> that one host's plugins + MCP servers (404 unknown /
  non-agent-runner machine, 503 offline / timed out).
- PUT writes toggles to `machine` (required; must be an agent-runner).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from base.db import Database
from gateway.cluster.roster import snapshots
from gateway.extensions.inventory_matrix import collapse_inventory, plan_dials
from gateway.extensions.schemas import (
    InventoryAggregate,
    InventoryItem,
    InventoryItemWriteResult,
    InventoryMachineView,
    InventoryWriteResult,
)
from ops.cluster import rpc as cluster_rpc
from ops.rpc_schemas import InventoryReadResult, InventoryWriteOpResult

router = APIRouter()
_log = logging.getLogger(__name__)


def _assert_inventory_target(pool: ConnectionPool, target: str) -> None:
    """404 unless `target` is a registered agent-runner.

    Inventory is agent-runner-only: a gateway name — including this gateway's
    own — has no plugin/MCP inventory and is rejected the same as a typo'd one. A
    registered-but-offline agent-runner passes here and 503s later on timeout.
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM machines WHERE name = %s AND 'agent-runner' = ANY(role)", (target,)
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"unknown agent-runner: {target!r}")


async def _dispatch_inventory_read(
    db: Database, target: str, *, timeout_s: float | None = None, retries: int | None = None
) -> InventoryReadResult:
    """Run inventory_read on agent-runner `target` by POSTing to its ops server.

    Targets are always remote agent-runners (the gateway is rejected upstream
    by `_assert_inventory_target`); the agent-runner's ops server executes
    inventory_read_op in-process. The wire dict is validated into an
    InventoryReadResult before it is returned.

    Does NOT convert the remote failure into an HTTPException: the aggregate
    catches the raw ClusterOpUnreachable/ClusterOpFailed per host (to bucket it
    into `unreachable`), and the single-machine GET converts it to 503 at its
    own call site. `retries` is forwarded to the cluster RPC retry cap — the
    aggregate passes 1 (one fast retry inside its budget), the single-machine
    view leaves the cluster default.
    """
    return InventoryReadResult.model_validate(
        await cluster_rpc.dispatch_to_machine(
            db,
            target_machine=target,
            kind="inventory_read",
            payload={},
            timeout_s=timeout_s,
            retries=retries,
        )
    )


async def _dispatch_inventory_write(
    db: Database, target: str, plugins: dict[str, bool], mcp_servers: dict[str, bool]
) -> InventoryWriteOpResult:
    """Run inventory_write on agent-runner `target` by POSTing to its ops server.

    Caller has already verified the target is a registered agent-runner."""
    try:
        wire = await cluster_rpc.dispatch_to_machine(
            db,
            target_machine=target,
            kind="inventory_write",
            payload={"plugins": plugins, "mcp_servers": mcp_servers},
        )
    except (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"machine {target!r} unreachable or failed the inventory request: {exc!s}",
        ) from exc
    return InventoryWriteOpResult.model_validate(wire)


def _agent_runner_rows(pool: ConnectionPool) -> list[tuple[str, bool]]:
    """Agent-runner rows — the aggregate's column set plus each row's
    intentionally-stopped latch.

    The gateway this gateway runs on is excluded by the role filter even
    though it is the local machine: inventory is agent-runner-only. An agent-runner
    that has not finished its startup UPSERT simply doesn't appear until it
    registers. `stopped_at` marks an intentional `meiva stop`; a stopped host
    stays a column but is reported unreachable without a dial (task #4127).
    """
    with pool.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT name, stopped_at IS NOT NULL FROM machines "
            "WHERE 'agent-runner' = ANY(role) ORDER BY name"
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


# meiva:note
# Dial one host under a bounded transport policy.
#
# The whole dial — one fast retry included — is capped by the aggregate read
# budget (`GatewayConfig.inventory_host_wait_seconds`), mirroring
# `roster_probe.dispatch_status_probe`. Without the cap, a blackholed host's dial burns the
# full budget across the cluster RPC retry chain and drags the aggregate tail to tens of
# seconds (task #4127). A host that already carries reachability failures gets the roster's
# fast-fail budget instead of the default deadline.
async def _dial_inventory_read(
    db: Database, target: str, *, budget_s: float
) -> InventoryReadResult:
    try:
        async with asyncio.timeout(budget_s):
            return await _dispatch_inventory_read(db, target, timeout_s=budget_s, retries=1)
    except TimeoutError as exc:
        raise cluster_rpc.ClusterOpUnreachable(
            f"inventory_read for machine={target!r} exceeded its {budget_s:.1f}s total budget"
        ) from exc


# meiva:note
# Fan out inventory_read to every agent-runner concurrently and collapse the
# results; a host whose read raised goes in `unreachable`.
#
# `plan_dials` decides which hosts are dialed; the rest report `unreachable` directly.
# Dialed hosts run under a bounded transport policy.
async def _aggregate(db: Database, pool: ConnectionPool, *, budget_s: float) -> InventoryAggregate:
    rows = _agent_runner_rows(pool)
    machines = [name for name, _stopped in rows]
    plan = plan_dials(
        rows, await asyncio.to_thread(snapshots.read_all_blocking, pool), now=datetime.now(UTC)
    )
    if plan.skipped:
        _log.debug("inventory aggregate: not dialing known-down host(s) %s", sorted(plan.skipped))

    results = await asyncio.gather(
        *(_dial_inventory_read(db, m, budget_s=budget_s) for m in plan.to_dial),
        return_exceptions=True,
    )
    reads: dict[str, InventoryReadResult] = {}
    unreachable: list[str] = sorted(plan.skipped)
    for m, res in zip(plan.to_dial, results, strict=True):
        if isinstance(res, (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed)):
            # The host couldn't serve its inventory (unreachable, or it answered and
            # its op failed) — bucket it.
            _log.warning("inventory_read on %s failed: %s", m, res)
            unreachable.append(m)
        elif isinstance(res, BaseException):
            # An unexpected exception (a bug in the dispatch helper, a
            # CancelledError) — do NOT relabel it as infrastructure flakiness.
            raise res
        else:
            reads[m] = res
    return collapse_inventory(reads, machines, unreachable)


@router.get("/api/inventory")
async def get_inventory(
    request: Request, machine: str | None = None
) -> InventoryAggregate | InventoryMachineView:
    """Return the plugin + MCP enable inventory.

    Without `machine`: the cross-machine AGGREGATE matrix (one row per plugin /
    MCP server, one cell per reachable agent-runner; failed hosts in `unreachable`).
    With `machine`: that one agent-runner's flat plugin + MCP lists (404 unknown /
    non-agent-runner machine, 503 offline / timed out).
    """
    if machine is None:
        return await _aggregate(
            request.app.state.db,
            request.app.state.db_pool,
            budget_s=request.app.state.gateway_config.inventory_host_wait_seconds,
        )

    await asyncio.to_thread(_assert_inventory_target, request.app.state.db_pool, machine)
    try:
        read = await _dispatch_inventory_read(request.app.state.db, machine)
    except (cluster_rpc.ClusterOpUnreachable, cluster_rpc.ClusterOpFailed) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"machine {machine!r} unreachable or failed the inventory request: {exc!s}",
        ) from exc

    plugins = [
        InventoryItem(
            name=name,
            kind="plugin",
            enabled=item.enabled,
            can_enable=item.can_enable,
            reason=item.reason,
            description=item.description,
        )
        for name, item in sorted(read.plugins.items())
    ]
    mcp_servers = [
        InventoryItem(
            name=name,
            kind="mcp",
            enabled=item.enabled,
            can_enable=item.can_enable,
            reason=item.reason,
            description=item.description,
        )
        for name, item in sorted(read.mcp_servers.items())
    ]
    return InventoryMachineView(machine=read.machine, plugins=plugins, mcp_servers=mcp_servers)


class InventoryWriteRequest(BaseModel):
    """PUT /api/inventory body. Each half is genuinely optional (the frontend may
    toggle only a plugin or only an MCP server), so a missing half defaults to
    empty — not the contract-required-field fallback AGENTS.md forbids. FastAPI
    422s a present-but-non-object half before the handler runs."""

    plugins: dict[str, bool] = {}
    mcp_servers: dict[str, bool] = {}


@router.put("/api/inventory")
async def put_inventory(
    body: InventoryWriteRequest, request: Request, machine: str | None = None
) -> InventoryWriteResult:
    """Apply plugin + MCP enable toggles to agent-runner `machine` (required).

    `machine` is required: inventory lives only on agent-runners, so there is no
    gateway default to write to (400 if absent, 404 if not an agent-runner).
    Body shape `{"plugins": {name: bool}, "mcp_servers": {name: bool}}` — the
    request model validates each half's shape (422 on a non-object half). The
    host-side op is the authoritative per-item gate; the result surfaces its
    verdicts + atomic `applied` flag.
    """
    if machine is None:
        raise HTTPException(
            status_code=400, detail="inventory writes require ?machine=<agent-runner>"
        )
    await asyncio.to_thread(_assert_inventory_target, request.app.state.db_pool, machine)

    result = await _dispatch_inventory_write(
        request.app.state.db, machine, body.plugins, body.mcp_servers
    )
    return InventoryWriteResult(
        applied=result.applied,
        plugin_results={
            name: InventoryItemWriteResult(ok=r.ok, reason=r.reason)
            for name, r in result.plugin_results.items()
        },
        mcp_results={
            name: InventoryItemWriteResult(ok=r.ok, reason=r.reason)
            for name, r in result.mcp_results.items()
        },
    )
