# meiva:note
# Transfer collaborators are composed from typed Gateway state at the request boundary.
from __future__ import annotations

import random
from dataclasses import dataclass

from fastapi import Request
from psycopg_pool import ConnectionPool

from base.cluster.machine import GatewayAccess
from base.db import Database
from base.events.live.bus import EventBus
from gateway.agents.transfer.bundle_store import TransferBundleStore
from gateway.http.config import (
    EnsureHarnesses,
    GatewayAppState,
    gateway_ensure_harnesses,
    gateway_rpc,
    state_as,
)
from ops.agent_transfer import WorkspaceTransferLimits
from ops.agent_transfer.limits import workspace_transfer_limits_from_read_model
from ops.cluster.rpc import RpcClient


@dataclass(frozen=True)
class TransferDeps:
    db_pool: ConnectionPool
    db: Database
    rpc: RpcClient
    bus: EventBus
    bundles: TransferBundleStore
    access: GatewayAccess
    rng: random.Random
    ensure_harnesses: EnsureHarnesses
    # meiva:note
    # The ceilings the runners enforce, read from the same cluster-pinned pair so the gateway refuses
    # exactly what the source was configured to send and the target is configured to accept.
    limits: WorkspaceTransferLimits


# meiva:note
# The staging store the app root builds for in-flight transfer bundles; this directory's route
# serves from it, so the directory declares it.
class TransferAppState(GatewayAppState):
    agent_transfer_bundles: TransferBundleStore


def transfer_deps(request: Request) -> TransferDeps:
    state = state_as(request, TransferAppState)
    return TransferDeps(
        db_pool=state.db_pool,
        db=state.db,
        rpc=gateway_rpc(request),
        bus=state.bus,
        bundles=state.agent_transfer_bundles,
        access=state.gateway_access,
        rng=state.gateway_config.rng,
        limits=workspace_transfer_limits_from_read_model(state.config_authority),
        ensure_harnesses=gateway_ensure_harnesses(request),
    )
