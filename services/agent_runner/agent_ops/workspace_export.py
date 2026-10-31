# meiva:note
# Binary `POST /workspace/export` route on `agent-ops`: validates a small `WorkspaceExportRequest`
# JSON body and streams back the self-verifying `MEIVA_WS_V1` archive of `<home>/workspaces/<id>`
# after verifying the agent's Postgres placement and non-running fence.
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from ops.agent_transfer import (
    WorkspaceTransferConflictError,
    WorkspaceTransferCorruptError,
    WorkspaceTransferTooLargeError,
)
from ops.agent_transfer.operations import export_workspace_bundle
from ops.rpc_schemas import WorkspaceExportRequest
from services.agent_runner.agent_ops import in_flight


def workspace_export_response(
    body: bytes, *, pool: ConnectionPool, machine: str, home: Path
) -> tuple[int, bytes, str]:
    try:
        request = WorkspaceExportRequest.model_validate_json(body)
        bundle = export_workspace_bundle(pool, request, local_machine=machine, home=home)
    except ValidationError:
        return 422, b"Invalid workspace export request", "text/plain"
    except WorkspaceTransferConflictError as exc:
        return 409, str(exc).encode("utf-8"), "text/plain"
    except WorkspaceTransferTooLargeError as exc:
        return 413, str(exc).encode("utf-8"), "text/plain"
    except WorkspaceTransferCorruptError as exc:
        return 422, str(exc).encode("utf-8"), "text/plain"
    return 200, bundle, "application/octet-stream"


async def workspace_export_route(
    body: bytes,
    *,
    pool: ConnectionPool,
    machine: str,
    home: Path,
    executor: ThreadPoolExecutor,
    dispatch_sem: asyncio.Semaphore,
    requests: in_flight.RequestTokens,
    workers: in_flight.WorkerFutures,
) -> tuple[int, bytes, str]:
    with in_flight.admitted(requests=requests):
        async with dispatch_sem:
            future = asyncio.get_running_loop().run_in_executor(
                executor,
                functools.partial(
                    workspace_export_response, body, pool=pool, machine=machine, home=home
                ),
            )
            in_flight.track_worker(future, workers=workers)
            return await asyncio.shield(future)
