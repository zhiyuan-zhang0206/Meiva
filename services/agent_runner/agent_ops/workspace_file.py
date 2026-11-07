# meiva:note
# Binary `POST /workspace-file/content` and `POST /workspace-file/raw` routes on `agent-ops`: the
# agent's own machine answers what the gateway would otherwise read from its own disk, so a file
# viewer works for an agent that runs anywhere. Both routes resolve the path through the same
# `base.agents.workspace_files` reader the local path uses -- one meaning for `path` whichever side
# answers.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from base.agents.workspace_files import (
    WorkspaceFile,
    WorkspaceFileBytes,
    WorkspaceFileFailure,
    WorkspaceFileRefused,
    WorkspaceFileTooLarge,
    read_workspace_file,
    read_workspace_file_bytes,
)
from base.db.rows import query_one
from services.agent_runner.agent_ops import in_flight

Handler = Callable[..., tuple[int, bytes, str]]


class WorkspaceFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: int = Field(gt=0)
    path: str = Field(min_length=1)
    max_bytes: int = Field(gt=0)


@dataclass(frozen=True)
class _MachineRow:
    machine: str


# meiva:note
# A refusal's status is the class itself: the gateway forwards it unchanged, so the two sides cannot
# disagree about what "not found" or "outside the roots" means, and no adapter has to translate a
# reason string back into a number.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
_STATUS: dict[WorkspaceFileFailure, int] = {
    WorkspaceFileFailure.PATH_INVALID: 400,
    WorkspaceFileFailure.OUTSIDE_ROOTS: 403,
    WorkspaceFileFailure.NOT_FOUND: 404,
    WorkspaceFileFailure.TOO_LARGE: 413,
}


# meiva:note
# The file lives on the machine that owns the agent, and the gateway may route by a stale row:
# answering here for another machine's agent would serve a same-named file from this disk.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def _owned_agent(pool: ConnectionPool, agent_id: int, machine: str) -> bool:
    with pool.connection() as conn:
        row = query_one(
            conn, _MachineRow, "SELECT machine FROM agents_meta WHERE id = %s", (agent_id,)
        )
    return row is not None and row.machine == machine


def _refusal(exc: WorkspaceFileRefused) -> tuple[int, bytes, str]:
    return _STATUS[exc.failure], exc.detail.encode(), "text/plain"


def content_response(
    body: bytes, *, pool: ConnectionPool, machine: str, home: Path, max_bytes: int
) -> tuple[int, bytes, str]:
    try:
        request = WorkspaceFileRequest.model_validate_json(body)
        if not _owned_agent(pool, request.agent_id, machine):
            return 404, b"Agent does not run on this machine", "text/plain"
        found: WorkspaceFile = read_workspace_file(
            home,
            request.agent_id,
            custom_cwd=None,
            raw_path=request.path,
            max_bytes=min(request.max_bytes, max_bytes),
        )
    except ValidationError:
        return 422, b"Invalid workspace file request", "text/plain"
    except WorkspaceFileTooLarge as exc:
        return _refusal(exc)
    except WorkspaceFileRefused as exc:
        return _refusal(exc)
    # meiva:note
    # The body is the gateway's own response shape, field for field, so the remote branch returns
    # exactly what the local one builds and a client cannot tell which machine answered.
    # meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
    return (
        200,
        json.dumps(
            {
                "path": found.path,
                "name": found.name,
                "extension": found.extension,
                "content": found.content,
                "size": found.size,
                "binary": found.binary,
                "modified_at": found.modified_at.isoformat(),
            }
        ).encode(),
        "application/json",
    )


def raw_response(
    body: bytes, *, pool: ConnectionPool, machine: str, home: Path, max_bytes: int
) -> tuple[int, bytes, str]:
    try:
        request = WorkspaceFileRequest.model_validate_json(body)
        if not _owned_agent(pool, request.agent_id, machine):
            return 404, b"Agent does not run on this machine", "text/plain"
        found: WorkspaceFileBytes = read_workspace_file_bytes(
            home,
            request.agent_id,
            custom_cwd=None,
            raw_path=request.path,
            max_bytes=min(request.max_bytes, max_bytes),
        )
    except ValidationError:
        return 422, b"Invalid workspace file request", "text/plain"
    except WorkspaceFileRefused as exc:
        return _refusal(exc)
    return 200, found.content, "application/octet-stream"


async def _dispatch(
    handler: Handler,
    body: bytes,
    *,
    pool: ConnectionPool,
    machine: str,
    home: Path,
    max_bytes: int,
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
                    handler, body, pool=pool, machine=machine, home=home, max_bytes=max_bytes
                ),
            )
            in_flight.track_worker(future, workers=workers)
            return await asyncio.shield(future)


async def content_route(
    body: bytes,
    *,
    pool: ConnectionPool,
    machine: str,
    home: Path,
    max_bytes: int,
    executor: ThreadPoolExecutor,
    dispatch_sem: asyncio.Semaphore,
    requests: in_flight.RequestTokens,
    workers: in_flight.WorkerFutures,
) -> tuple[int, bytes, str]:
    return await _dispatch(
        content_response,
        body,
        pool=pool,
        machine=machine,
        home=home,
        max_bytes=max_bytes,
        executor=executor,
        dispatch_sem=dispatch_sem,
        requests=requests,
        workers=workers,
    )


async def raw_route(
    body: bytes,
    *,
    pool: ConnectionPool,
    machine: str,
    home: Path,
    max_bytes: int,
    executor: ThreadPoolExecutor,
    dispatch_sem: asyncio.Semaphore,
    requests: in_flight.RequestTokens,
    workers: in_flight.WorkerFutures,
) -> tuple[int, bytes, str]:
    return await _dispatch(
        raw_response,
        body,
        pool=pool,
        machine=machine,
        home=home,
        max_bytes=max_bytes,
        executor=executor,
        dispatch_sem=dispatch_sem,
        requests=requests,
        workers=workers,
    )
