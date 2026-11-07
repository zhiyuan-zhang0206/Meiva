import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from psycopg_pool import ConnectionPool
from pydantic import ValidationError

from base.agents.to_user import (
    ToUserLimits,
    ToUserReadFailure,
    ToUserReadRequest,
    WorkspaceFileRequest,
    read_failure,
    read_resource,
)
from ops.to_user import (
    WorkspaceFileError,
    owned_workspace,
    read_workspace_file_content,
    read_workspace_file_raw,
)
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
    body: bytes, *, pool: ConnectionPool, machine: str, home: Path, limits: ToUserLimits
) -> tuple[int, bytes, str]:

    try:
        request = ToUserReadRequest.model_validate_json(body)
        workspace = owned_workspace(pool, request.agent_id, machine, home=home)
        content = read_resource(workspace, request.files, limits=limits)
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
    limits: ToUserLimits,
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
                    resource_response, body, pool=pool, machine=machine, home=home, limits=limits
                ),
            )
            in_flight.track_worker(future, workers=workers)
            return await asyncio.shield(future)


def workspace_file_response(
    body: bytes, *, pool: ConnectionPool, machine: str, home: Path, limits: ToUserLimits
) -> tuple[int, bytes, str]:

    try:
        request = WorkspaceFileRequest.model_validate_json(body)
        owned_workspace(pool, request.agent_id, machine, home=home)
        if request.mode == "content":
            payload = read_workspace_file_content(
                home, request.agent_id, request.path, custom_cwd=request.custom_cwd
            )
            return 200, payload.model_dump_json().encode("utf-8"), "application/json"
        raw = read_workspace_file_raw(
            home,
            request.agent_id,
            request.path,
            custom_cwd=request.custom_cwd,
            max_bytes=limits.resource_bytes,
        )
    except ValidationError:
        return 422, b"Invalid workspace file request", "text/plain"
    except WorkspaceFileError as exc:
        return exc.status_code, exc.detail.encode("utf-8"), "text/plain"
    except ValueError as exc:
        return 404, str(exc).encode("utf-8"), "text/plain"
    except OSError:
        return 422, b"Workspace file could not be read", "text/plain"
    return 200, raw, "application/octet-stream"


async def workspace_file_route(
    body: bytes,
    *,
    pool: ConnectionPool,
    machine: str,
    home: Path,
    limits: ToUserLimits,
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
                    workspace_file_response,
                    body,
                    pool=pool,
                    machine=machine,
                    home=home,
                    limits=limits,
                ),
            )
            in_flight.track_worker(future, workers=workers)
            return await asyncio.shield(future)
