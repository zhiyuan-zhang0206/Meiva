import asyncio
import contextlib
import logging
import mimetypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import httpx
import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from base.agents.to_user import ToUserDocument, ToUserReadFailure, ToUserReadRequest
from base.agents.workspace_files import (
    WorkspaceFileFailure,
    WorkspaceFileRefused,
    clean_file_path,
    locate_workspace_file,
    read_workspace_file,
)
from base.cluster.machine import gateway_auth_headers
from base.cluster.machines import MachineGatewayUrlMissing, MachineNotRegistered, lookup
from base.db.rows import query_one
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.http.config import gateway_config, gateway_rpc, gateway_state
from gateway.http.middleware.error_envelope import error_response
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable

router = APIRouter()
_log = logging.getLogger(__name__)

# meiva:link gateway/agents/docs/to-user-read-failures.meiva.okf.md
_READ_FAILURES: dict[ToUserReadFailure, tuple[int, str, str]] = {
    ToUserReadFailure.GONE: (
        404,
        "to_user_reference_gone",
        "The document no longer references this file",
    ),
    ToUserReadFailure.TOO_LARGE: (
        413,
        "to_user_document_too_large",
        "The file is larger than the panel's document budget",
    ),
    ToUserReadFailure.UNREADABLE: (
        422,
        "to_user_document_unreadable",
        "The workspace document could not be read",
    ),
}


# meiva:note
# The route answers a refusal itself: the class names the envelope code, which is
# what the panel reads, so the status is only how the refusal travels.
# meiva:link gateway/agents/docs/to-user-read-failures.meiva.okf.md
class PanelReadFailed(Exception):  # noqa: N818
    def __init__(self, failure: ToUserReadFailure) -> None:
        super().__init__(failure.value)
        self.status, self.code, self.detail = _READ_FAILURES[failure]

    def response(self, request: Request) -> JSONResponse:
        return error_response(
            request, code=self.code, status=self.status, detail=self.detail, retryable=False
        )


# meiva:note
# The machine an agent lives on.
@dataclass(frozen=True)
class _MachineRow:
    machine: str


@dataclass(frozen=True)
class _CwdRow:
    cwd: str | None = None


def home_machine(request: Request, agent_id: int) -> str:
    deny_isolated_result_read(request)
    with gateway_state(request).db_pool.connection() as conn:
        row = query_one(
            conn, _MachineRow, "SELECT machine FROM agents_meta WHERE id = %s", (agent_id,)
        )
    if row is None:
        raise HTTPException(404, "Agent not found")
    return row.machine


def resource_request(agent_id: int, files: list[str], *, max_depth: int) -> ToUserReadRequest:
    if len(files) > max_depth:
        raise HTTPException(422, f"A resource chain may carry at most {max_depth} file names")
    try:
        return ToUserReadRequest(agent_id=agent_id, files=files)
    except ValidationError as exc:
        raise HTTPException(422, "Invalid document file name") from exc


async def read_snapshot(request: Request, query: ToUserReadRequest, machine: str) -> ToUserDocument:
    try:
        result = await gateway_rpc(request).dispatch_to_machine(
            gateway_state(request).db,
            target_machine=machine,
            kind="to_user_read",
            payload=query.model_dump(),
            timeout_s=3,
            retries=0,
        )
    except ClusterOpUnreachable as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc
    except ClusterOpFailed as exc:
        try:
            failure = ToUserReadFailure(exc.result.get("reason"))
        except ValueError:
            _log.warning("to_user_read failed without a known reason: %s", exc.result.get("error"))
            failure = ToUserReadFailure.UNREADABLE
        raise PanelReadFailed(failure) from exc
    return ToUserDocument.model_validate(result)


@router.get("/api/agents/{agent_id}/to-user", response_model=ToUserDocument)
async def get_to_user(
    agent_id: int,
    request: Request,
    file: Annotated[list[str] | None, Query()] = None,
) -> ToUserDocument | JSONResponse:
    machine = await asyncio.to_thread(home_machine, request, agent_id)
    max_depth = gateway_config(request).to_user.max_chain_depth
    query = resource_request(agent_id, file or [], max_depth=max_depth)
    try:
        return await read_snapshot(request, query, machine)
    except PanelReadFailed as failed:
        return failed.response(request)


_PASSIVE_PREFIXES = ("image/", "audio/", "video/")
_PASSIVE_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "application/xml",
        "text/csv",
        "text/markdown",
        "text/plain",
        "text/xml",
    }
)

_HTML_TYPES = frozenset({"text/html"})

_HTML_POLICY = (
    "sandbox allow-scripts; default-src 'none'; "
    "img-src data: https:; media-src data: https:; font-src data: https:; "
    "style-src 'unsafe-inline' data: https:; script-src 'unsafe-inline' data: https:; "
    "form-action 'none'; base-uri 'none'"
)
_PASSIVE_POLICY = "sandbox; default-src 'none'; style-src 'unsafe-inline'"


# meiva:link gateway/agents/docs/to-user-resources.meiva.okf.md
def response_policy(media_type: str) -> tuple[str, str]:

    if media_type in _HTML_TYPES:
        return "inline", _HTML_POLICY
    passive = media_type.startswith(_PASSIVE_PREFIXES) or media_type in _PASSIVE_TYPES
    return ("inline" if passive else "attachment"), _PASSIVE_POLICY


def resource_response(content: bytes, name: str) -> Response:

    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    disposition, policy = response_policy(media_type)
    return Response(
        content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": policy,
            "Referrer-Policy": "no-referrer",
        },
    )


async def fetch_resource(
    client: httpx.AsyncClient,
    url: str,
    query: ToUserReadRequest,
    headers: dict[str, str],
    *,
    limit: int,
) -> bytes:

    async with client.stream("POST", url, json=query.model_dump(), headers=headers) as response:
        if response.status_code != 200:
            status = response.status_code if response.status_code in (404, 413, 422, 503) else 502
            raise HTTPException(status, "Referenced file could not be read")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > limit:
                raise HTTPException(413, f"Referenced file exceeds the {limit}-byte resource limit")
        return bytes(content)


@router.get("/api/agents/{agent_id}/to-user/resource", response_model=None)
async def get_resource(
    agent_id: int,
    request: Request,
    file: Annotated[list[str], Query(min_length=1)],
) -> Response | JSONResponse:
    machine = await asyncio.to_thread(home_machine, request, agent_id)
    to_user = gateway_config(request).to_user
    query = resource_request(agent_id, file, max_depth=to_user.max_chain_depth)
    parent = resource_request(agent_id, query.files[:-1], max_depth=to_user.max_chain_depth)
    try:
        document = await read_snapshot(request, parent, machine)
    except PanelReadFailed as failed:
        return failed.response(request)
    resource = next(
        (value for value in document.resources.values() if value.file == file[-1]), None
    )
    if resource is None:
        raise HTTPException(404, "The document no longer references this resource")
    access = gateway_state(request).gateway_access
    try:
        ops_url = await asyncio.to_thread(lookup, gateway_state(request).db, machine)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=3), trust_env=False
        ) as client:
            content = await fetch_resource(
                client,
                f"{ops_url.rstrip('/')}/to-user/resource",
                query,
                gateway_auth_headers(access),
                limit=to_user.max_resource_bytes,
            )
    except (httpx.HTTPError, MachineGatewayUrlMissing, MachineNotRegistered) as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc
    return resource_response(content, resource.name)


class WorkspaceFileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    name: str
    extension: str
    content: str
    size: int
    binary: bool
    modified_at: datetime


MAX_WORKSPACE_FILE_BYTES = 1024 * 1024


def _query_agent_cwd(conn: psycopg.Connection, agent_id: int) -> str | None:
    with contextlib.suppress(psycopg.Error):
        row = query_one(
            conn,
            _CwdRow,
            """
            SELECT COALESCE(
                checkpoint -> 'channel_values' ->> 'meiva_code__cwd',
                checkpoint -> 'channel_values' ->> 'cwd'
            ) AS cwd
            FROM checkpoints
            WHERE thread_id = %s
            ORDER BY checkpoint_id DESC
            LIMIT 1
            """,
            (str(agent_id),),
        )
        if row is not None:
            return row.cwd
    return None


# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
_WORKSPACE_FILE_STATUS: dict[WorkspaceFileFailure, int] = {
    WorkspaceFileFailure.PATH_INVALID: 400,
    WorkspaceFileFailure.OUTSIDE_ROOTS: 403,
    WorkspaceFileFailure.NOT_FOUND: 404,
    WorkspaceFileFailure.TOO_LARGE: 413,
}


def _local_machine(request: Request) -> str:
    return gateway_state(request).machine.name()


# meiva:note
# The raw route needs the resolved path, not the payload: it streams from disk when the file is here,
# and shares the resolver with the content route so both answer the same path with the same class.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def _workspace_file_path(request: Request, agent_id: int, path: str) -> Path:
    with gateway_state(request).db_pool.connection() as conn:
        custom_cwd = _query_agent_cwd(conn, agent_id)
    home = gateway_state(request).gateway_process.home
    try:
        target, _ = locate_workspace_file(home, agent_id, custom_cwd=custom_cwd, raw_path=path)
    except WorkspaceFileRefused as exc:
        raise HTTPException(_WORKSPACE_FILE_STATUS[exc.failure], exc.detail) from exc
    return target


def _workspace_file_from_disk(request: Request, agent_id: int, path: str) -> WorkspaceFileResponse:
    with gateway_state(request).db_pool.connection() as conn:
        custom_cwd = _query_agent_cwd(conn, agent_id)
    home = gateway_state(request).gateway_process.home
    try:
        found = read_workspace_file(
            home,
            agent_id,
            custom_cwd=custom_cwd,
            raw_path=path,
            max_bytes=MAX_WORKSPACE_FILE_BYTES,
        )
    except WorkspaceFileRefused as exc:
        raise HTTPException(_WORKSPACE_FILE_STATUS[exc.failure], exc.detail) from exc
    return WorkspaceFileResponse(
        path=found.path,
        name=found.name,
        extension=found.extension,
        content=found.content,
        size=found.size,
        binary=found.binary,
        modified_at=found.modified_at,
    )


# meiva:note
# The one way this gateway reaches another machine's agent-ops. A refused read is forwarded with the
# status the owning machine chose -- those are the four the table above names -- and anything else is
# this gateway's own failure to reach it, which is a 503 and not a verdict about the file.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
async def _ask_agent_machine(
    request: Request, machine: str, route: str, payload: dict[str, object]
) -> httpx.Response:
    access = gateway_state(request).gateway_access
    try:
        ops_url = await asyncio.to_thread(lookup, gateway_state(request).db, machine)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=3), trust_env=False
        ) as client:
            return await client.post(
                f"{ops_url.rstrip('/')}/{route}",
                json=payload,
                headers=gateway_auth_headers(access),
            )
    except (httpx.HTTPError, MachineGatewayUrlMissing, MachineNotRegistered) as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc


def _forwarded_status(response: httpx.Response) -> int:
    status = response.status_code
    return status if status in _WORKSPACE_FILE_STATUS.values() else 502


@router.get("/api/agents/{agent_id}/files/content", response_model=WorkspaceFileResponse)
async def get_workspace_file_content(
    agent_id: int,
    request: Request,
    path: Annotated[
        str, Query(description="Workspace or cwd relative path, absolute path, or file:// URL")
    ],
) -> WorkspaceFileResponse:
    machine = await asyncio.to_thread(home_machine, request, agent_id)
    if machine == _local_machine(request):
        return await asyncio.to_thread(_workspace_file_from_disk, request, agent_id, path)
    response = await _ask_agent_machine(
        request,
        machine,
        "workspace-file/content",
        {
            "agent_id": agent_id,
            "path": path,
            "max_bytes": min(
                gateway_config(request).to_user.max_resource_bytes, MAX_WORKSPACE_FILE_BYTES
            ),
        },
    )
    if response.status_code != 200:
        raise HTTPException(_forwarded_status(response), response.text)
    return WorkspaceFileResponse.model_validate(response.json())


# meiva:note
# The headers are the same for a file read here and one fetched from another machine: the content
# policy follows the media type, never the route, so a remote file cannot be served with a weaker
# sandbox than an identical local one.
# meiva:link base/agents/workspace_files/docs/workspace-files.meiva.okf.md
def _raw_headers(name: str, media_type: str) -> dict[str, str]:
    disposition, policy = response_policy(media_type)
    return {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": policy,
        "Referrer-Policy": "no-referrer",
    }


@router.get("/api/agents/{agent_id}/files/raw", response_model=None)
async def get_workspace_file_raw(
    agent_id: int,
    request: Request,
    path: Annotated[
        str, Query(description="Workspace or cwd relative path, absolute path, or file:// URL")
    ],
) -> Response:
    machine = await asyncio.to_thread(home_machine, request, agent_id)
    if machine == _local_machine(request):
        target = await asyncio.to_thread(_workspace_file_path, request, agent_id, path)
        stat = target.stat()
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(
            path=target,
            stat_result=stat,
            media_type=media_type,
            headers=_raw_headers(target.name, media_type),
        )
    response = await _ask_agent_machine(
        request,
        machine,
        "workspace-file/raw",
        {
            "agent_id": agent_id,
            "path": path,
            "max_bytes": gateway_config(request).to_user.max_resource_bytes,
        },
    )
    if response.status_code != 200:
        raise HTTPException(_forwarded_status(response), response.text)
    media_type = str(response.headers.get("content-type") or "application/octet-stream")
    return Response(
        content=response.content,
        media_type=media_type,
        headers=_raw_headers(Path(clean_file_path(path)).name, media_type),
    )
