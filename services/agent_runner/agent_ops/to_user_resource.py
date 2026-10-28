import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.agents.to_user import (
    ToUserReadFailure,
    ToUserReadRequest,
    read_failure,
    read_resource,
)
from ops.to_user import owned_workspace
from services.agent_runner.agent_ops import in_flight

# meiva:note
# One answer per failure class, so a caller can tell "gone" (stop) from "too large"
# (ask for the file rather than the rendering) without reading the body text.
# meiva:link gateway/agents/docs/to-user-read-failures.meiva.okf.md
_STATUS: dict[ToUserReadFailure, tuple[int, bytes]] = {
    ToUserReadFailure.GONE: (404, b"Referenced file is no longer available"),
    ToUserReadFailure.TOO_LARGE: (413, b"Referenced file is larger than the resource budget"),
    ToUserReadFailure.UNREADABLE: (422, b"Referenced file could not be read"),
}


def resource_response(
    body: bytes, *, pool: ConnectionPool, machine: str, home: Path
) -> tuple[int, bytes, str]:

    try:
        request = ToUserReadRequest.model_validate_json(body)
        workspace = owned_workspace(pool, request.agent_id, machine, home=home)
        content = read_resource(workspace, request.references)
    except ValidationError:
        return 422, b"Invalid resource request", "text/plain"
    except (OSError, ValueError) as exc:
        status, text = _STATUS[read_failure(exc)]
        return status, text, "text/plain"
    return 200, content, "application/octet-stream"


async def resource_route(
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
                functools.partial(resource_response, body, pool=pool, machine=machine, home=home),
            )
            in_flight.track_worker(future, workers=workers)
            return await asyncio.shield(future)
