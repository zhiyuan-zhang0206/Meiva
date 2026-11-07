import asyncio
import contextlib
import logging
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import httpx
import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import ValidationError

from base.agents.to_user import (
    ToUserDocument,
    ToUserReadFailure,
    ToUserReadRequest,
    WorkspaceFileRequest,
    WorkspaceFileResponse,
)
from base.cluster.machine import gateway_auth_headers
from base.cluster.machines import MachineGatewayUrlMissing, MachineNotRegistered, lookup
from base.db.rows import query_one
from base.paths import workspace_path
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.http.config import gateway_config, gateway_rpc, gateway_state
from gateway.http.middleware.error_envelope import error_response
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.to_user import (
    WorkspaceFileError,
    clean_file_path,
    read_workspace_file_content,
    resolve_existing_workspace_file,
)

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


def _lookup_agent_placement(request: Request, agent_id: int) -> tuple[str, str | None]:
    deny_isolated_result_read(request)
    with gateway_state(request).db_pool.connection() as conn:
        row = query_one(
            conn, _MachineRow, "SELECT machine FROM agents_meta WHERE id = %s", (agent_id,)
        )
        if row is None:
            raise HTTPException(404, "Agent not found")
        custom_cwd = _query_agent_cwd(conn, agent_id)
    return row.machine, custom_cwd


async def _resolve_agent_ops_url(
    request: Request, agent_id: int, machine: str, custom_cwd: str | None
) -> str | None:
    state = gateway_state(request)
    if machine == state.machine.name():
        return None
    try:
        return await asyncio.to_thread(lookup, state.db, machine)
    except MachineNotRegistered as exc:
        home = state.gateway_process.home
        if workspace_path(home, agent_id).exists() or (
            custom_cwd is not None and Path(custom_cwd).exists()
        ):
            return None
        raise HTTPException(503, "The agent's machine is unavailable") from exc
    except MachineGatewayUrlMissing as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc


async def fetch_workspace_file_content(
    client: httpx.AsyncClient,
    url: str,
    query: WorkspaceFileRequest,
    headers: dict[str, str],
) -> WorkspaceFileResponse:
    response = await client.post(url, json=query.model_dump(), headers=headers)
    if response.status_code != 200:
        status = (
            response.status_code if response.status_code in (400, 403, 404, 413, 422, 503) else 502
        )
        detail = response.text.strip() or "Workspace file could not be read"
        raise HTTPException(status, detail)
    return WorkspaceFileResponse.model_validate_json(response.content)


async def fetch_workspace_file_raw(
    client: httpx.AsyncClient,
    url: str,
    query: WorkspaceFileRequest,
    headers: dict[str, str],
    *,
    limit: int,
) -> bytes:
    async with client.stream("POST", url, json=query.model_dump(), headers=headers) as response:
        if response.status_code != 200:
            raw_err = await response.aread()
            status = (
                response.status_code
                if response.status_code in (400, 403, 404, 413, 422, 503)
                else 502
            )
            detail = (
                raw_err.decode("utf-8", errors="replace").strip()
                or "Workspace file could not be read"
            )
            raise HTTPException(status, detail)
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > limit:
                raise HTTPException(413, f"Referenced file exceeds the {limit}-byte resource limit")
        return bytes(content)


@router.get("/api/agents/{agent_id}/files/content", response_model=WorkspaceFileResponse)
async def get_workspace_file_content(
    agent_id: int,
    request: Request,
    path: Annotated[
        str, Query(description="Workspace or cwd relative path, absolute path, or file:// URL")
    ],
) -> WorkspaceFileResponse:
    machine, custom_cwd = await asyncio.to_thread(_lookup_agent_placement, request, agent_id)
    try:
        _ = clean_file_path(path)
    except WorkspaceFileError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    ops_url = await _resolve_agent_ops_url(request, agent_id, machine, custom_cwd)
    if ops_url is None:
        home = gateway_state(request).gateway_process.home
        try:
            return await asyncio.to_thread(
                read_workspace_file_content, home, agent_id, path, custom_cwd=custom_cwd
            )
        except WorkspaceFileError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    query = WorkspaceFileRequest(
        agent_id=agent_id, path=path, mode="content", custom_cwd=custom_cwd
    )
    access = gateway_state(request).gateway_access
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=3), trust_env=False
        ) as client:
            return await fetch_workspace_file_content(
                client,
                f"{ops_url.rstrip('/')}/to-user/workspace-file",
                query,
                gateway_auth_headers(access),
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc


@router.get("/api/agents/{agent_id}/files/raw", response_model=None)
async def get_workspace_file_raw(
    agent_id: int,
    request: Request,
    path: Annotated[
        str, Query(description="Workspace or cwd relative path, absolute path, or file:// URL")
    ],
) -> Response:
    machine, custom_cwd = await asyncio.to_thread(_lookup_agent_placement, request, agent_id)
    try:
        cleaned = clean_file_path(path)
    except WorkspaceFileError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    ops_url = await _resolve_agent_ops_url(request, agent_id, machine, custom_cwd)
    if ops_url is None:
        home = gateway_state(request).gateway_process.home
        try:
            target, _ = await asyncio.to_thread(
                resolve_existing_workspace_file, home, agent_id, path, custom_cwd=custom_cwd
            )
        except WorkspaceFileError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc
        stat = target.stat()
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        disposition, policy = response_policy(media_type)
        headers = {
            "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(target.name, safe='')}",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": policy,
            "Referrer-Policy": "no-referrer",
        }
        return FileResponse(
            path=target,
            stat_result=stat,
            media_type=media_type,
            headers=headers,
        )

    query = WorkspaceFileRequest(agent_id=agent_id, path=path, mode="raw", custom_cwd=custom_cwd)
    access = gateway_state(request).gateway_access
    to_user = gateway_config(request).to_user
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=3), trust_env=False
        ) as client:
            content = await fetch_workspace_file_raw(
                client,
                f"{ops_url.rstrip('/')}/to-user/workspace-file",
                query,
                gateway_auth_headers(access),
                limit=to_user.max_resource_bytes,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc
    return resource_response(content, Path(cleaned).name)
