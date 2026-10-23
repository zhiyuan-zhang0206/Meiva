# meiva:note
# POST one cluster op to a machine's ops server, mapping transport outcomes to HTTP.

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from base.db import Database
from ops.cluster import rpc as cluster_rpc
from ops.rpc_schemas import OpKind


# meiva:note
# POST one op to `target`'s ops server, mapping transport outcomes to HTTP.
#
# The single path every per-host cluster op takes — the local machine
# included (its ops server is dialed at its registered localhost URL); the
# gateway never runs session/pidfile operations itself.
#
# Raises:
#     HTTPException 503: the target's ops server was unreachable.
#     HTTPException 502: the op ran on the target but reported failure
#         (e.g. an update already in flight there).
async def dispatch_op(
    db: Database, target: str, kind: OpKind, payload: dict[str, Any]
) -> dict[str, Any]:
    try:
        return await cluster_rpc.dispatch_to_machine(
            db, target_machine=target, kind=kind, payload=payload
        )
    except cluster_rpc.ClusterOpUnreachable as exc:
        raise HTTPException(
            status_code=503,
            detail=f"machine {target!r} ops server unreachable for {kind}: {exc!s}",
        ) from exc
    except cluster_rpc.ClusterOpFailed as exc:
        raise HTTPException(
            status_code=502,
            detail=f"machine {target!r} {kind} failed: {exc.result!r}",
        ) from exc
