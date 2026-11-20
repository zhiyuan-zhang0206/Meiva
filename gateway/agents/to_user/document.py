import asyncio
import contextlib
import logging
import mimetypes
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import httpx
import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ValidationError
from starlette.background import BackgroundTask

from base.agents.to_user import (
    ToUserDocument,
    ToUserReadFailure,
    ToUserReadRequest,
    WorkspaceDirListing,
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
    list_workspace_dir,
    parse_explicit_workspace_path,
    read_workspace_file_content,
    resolve_existing_workspace_file,
)

router = APIRouter()
_log = logging.getLogger(__name__)

# meiva:link gateway/agents/docs/to-user-read-failures.md
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
# meiva:link gateway/agents/docs/to-user-read-failures.md
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

_PASSIVE_POLICY = "sandbox; default-src 'none'; style-src 'unsafe-inline'"


# meiva:link gateway/agents/docs/to-user-resources.md
def response_policy(media_type: str) -> tuple[str, str]:

    passive = media_type.startswith(_PASSIVE_PREFIXES) or media_type in _PASSIVE_TYPES
    return ("inline" if passive else "attachment"), _PASSIVE_POLICY


# meiva:note
# The media type a file name implies and the headers every served file carries: its disposition
# and the passive CSP, never cached, never sniffed into another type.
def passive_headers(name: str) -> tuple[str, dict[str, str]]:
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    disposition, policy = response_policy(media_type)
    return media_type, {
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(name, safe='')}",
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": policy,
        "Referrer-Policy": "no-referrer",
    }


def resource_response(content: bytes, name: str) -> Response:

    media_type, headers = passive_headers(name)
    return Response(content, media_type=media_type, headers=headers)


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


# meiva:note
# Eval isolation is checked against the caller first; then an explicit
# `.meiva/workspaces/<owner>/<subpath>` reference switches placement lookup to `<owner>`
# with `custom_cwd=None` so cross-agent links resolve on `<owner>`'s machine regardless
# of which agent's conversation opened the link. A directory read (`allow_root`) may name the
# workspace root itself, and no path at all names the agent's own workspace root (`""`).
# meiva:link gateway/agents/docs/to-user-resources.md
def _resolve_workspace_file_request(
    request: Request, agent_id: int, raw_path: str | None, *, allow_root: bool = False
) -> tuple[int, str, str, str, str | None]:
    deny_isolated_result_read(request)
    home = gateway_state(request).gateway_process.home
    cleaned = ""
    explicit = None
    if raw_path is not None:
        try:
            cleaned = clean_file_path(raw_path)
            explicit = parse_explicit_workspace_path(raw_path, home=home, allow_root=allow_root)
        except WorkspaceFileError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    target_agent_id = explicit[0] if explicit is not None else agent_id
    target_path = explicit[1] if explicit is not None else (raw_path or "")
    cleaned_name = Path(explicit[1] if explicit is not None else cleaned).name
    include_cwd = explicit is None

    with gateway_state(request).db_pool.connection() as conn:
        row = query_one(
            conn, _MachineRow, "SELECT machine FROM agents_meta WHERE id = %s", (target_agent_id,)
        )
        if row is None:
            raise HTTPException(404, "Agent not found")
        custom_cwd = _query_agent_cwd(conn, target_agent_id) if include_cwd else None
    return target_agent_id, target_path, cleaned_name, row.machine, custom_cwd


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


# meiva:note
# Where one workspace read happens: `ops_url` is None when the gateway host holds the files.
@dataclass(frozen=True)
class _WorkspaceTarget:
    agent_id: int
    path: str
    name: str
    custom_cwd: str | None
    ops_url: str | None


async def _workspace_target(
    request: Request, agent_id: int, raw_path: str | None, *, allow_root: bool = False
) -> _WorkspaceTarget:
    target_agent_id, target_path, name, machine, custom_cwd = await asyncio.to_thread(
        _resolve_workspace_file_request, request, agent_id, raw_path, allow_root=allow_root
    )
    ops_url = await _resolve_agent_ops_url(request, target_agent_id, machine, custom_cwd)
    return _WorkspaceTarget(target_agent_id, target_path, name, custom_cwd, ops_url)


async def _read_locally[**P, T](read: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    try:
        return await asyncio.to_thread(read, *args, **kwargs)
    except WorkspaceFileError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def _forward_status(status: int) -> int:
    return status if status in (400, 403, 404, 413, 422, 503) else 502


# meiva:note
# Unset fields stay off the wire, so a runner that predates `list`/`byte_range` still
# accepts the content and raw reads it already knew.
def forwarded_body(query: WorkspaceFileRequest) -> dict[str, object]:
    return query.model_dump(exclude_none=True)


async def fetch_workspace_json[Answer: BaseModel](
    client: httpx.AsyncClient,
    url: str,
    query: WorkspaceFileRequest,
    headers: dict[str, str],
    answer: type[Answer],
) -> Answer:
    response = await client.post(url, json=forwarded_body(query), headers=headers)
    if response.status_code != 200:
        detail = response.text.strip() or "Workspace file could not be read"
        raise HTTPException(_forward_status(response.status_code), detail)
    return answer.model_validate_json(response.content)


async def _forward_workspace_json[Answer: BaseModel](
    request: Request, ops_url: str, query: WorkspaceFileRequest, answer: type[Answer]
) -> Answer:
    access = gateway_state(request).gateway_access
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=3), trust_env=False
        ) as client:
            return await fetch_workspace_json(
                client,
                f"{ops_url.rstrip('/')}/to-user/workspace-file",
                query,
                gateway_auth_headers(access),
                answer,
            )
    except httpx.HTTPError as exc:
        raise HTTPException(503, "The agent's machine is unavailable") from exc


# meiva:note
# Open the owning runner's answer to a raw read without reading its body. `200`, `206` and `416`
# are the file's answer and relay as they are; anything else becomes the gateway's refusal. A
# whole file larger than `limit` is refused from its declared length before a byte is relayed.
# A runner that predates `byte_range` refuses the field as `422`; the read is then asked again
# without it, and the browser gets the whole file as it did before ranges were forwarded.
async def open_workspace_file_raw(
    client: httpx.AsyncClient,
    url: str,
    query: WorkspaceFileRequest,
    headers: dict[str, str],
    *,
    limit: int,
) -> httpx.Response:
    try:
        return await _open_raw_once(client, url, query, headers, limit=limit)
    except HTTPException as refused:
        if refused.status_code != 422 or query.byte_range is None:
            raise
    whole = query.model_copy(update={"byte_range": None})
    return await _open_raw_once(client, url, whole, headers, limit=limit)


async def _open_raw_once(
    client: httpx.AsyncClient,
    url: str,
    query: WorkspaceFileRequest,
    headers: dict[str, str],
    *,
    limit: int,
) -> httpx.Response:
    upstream = await client.send(
        client.build_request("POST", url, json=forwarded_body(query), headers=headers),
        stream=True,
    )
    if upstream.status_code not in (200, 206, 416):
        raw_err = await upstream.aread()
        await upstream.aclose()
        detail = raw_err.decode("utf-8", errors="replace").strip()
        raise HTTPException(
            _forward_status(upstream.status_code), detail or "Workspace file could not be read"
        )
    declared = upstream.headers.get_list("content-length")
    if declared and declared[0].isdigit() and int(declared[0]) > limit:
        await upstream.aclose()
        raise HTTPException(413, f"Referenced file exceeds the {limit}-byte resource limit")
    return upstream


async def relay_body(upstream: httpx.Response, *, limit: int) -> AsyncIterator[bytes]:
    relayed = 0
    try:
        async for chunk in upstream.aiter_bytes():
            relayed += len(chunk)
            if relayed > limit:
                return
            yield chunk
    finally:
        await upstream.aclose()


# meiva:note
# The forwarded raw read streams the runner's answer through instead of buffering it: the
# browser's `Range` travels as `byte_range`, and the runner's `Content-Range` and length come
# back with the file's own type and the passive headers.
async def _stream_workspace_raw(
    request: Request, target: _WorkspaceTarget, ops_url: str
) -> Response:
    query = WorkspaceFileRequest(
        agent_id=target.agent_id,
        path=target.path,
        mode="raw",
        custom_cwd=target.custom_cwd,
        byte_range=request.headers.get("range"),
    )
    limit = gateway_config(request).to_user.max_resource_bytes
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=3), trust_env=False)
    try:
        upstream = await open_workspace_file_raw(
            client,
            f"{ops_url.rstrip('/')}/to-user/workspace-file",
            query,
            gateway_auth_headers(gateway_state(request).gateway_access),
            limit=limit,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(503, "The agent's machine is unavailable") from exc
    except HTTPException:
        await client.aclose()
        raise
    media_type, headers = passive_headers(target.name)
    headers["Accept-Ranges"] = "bytes"
    for name in ("content-length", "content-range"):
        if name in upstream.headers:
            headers[name] = upstream.headers[name]
    return StreamingResponse(
        relay_body(upstream, limit=limit),
        status_code=upstream.status_code,
        headers=headers,
        media_type=media_type,
        background=BackgroundTask(client.aclose),
    )


@router.get("/api/agents/{agent_id}/files/content", response_model=WorkspaceFileResponse)
async def get_workspace_file_content(
    agent_id: int,
    request: Request,
    path: Annotated[
        str, Query(description="Workspace or cwd relative path, absolute path, or file:// URL")
    ],
) -> WorkspaceFileResponse:
    target = await _workspace_target(request, agent_id, path)
    if target.ops_url is None:
        return await _read_locally(
            read_workspace_file_content,
            gateway_state(request).gateway_process.home,
            target.agent_id,
            target.path,
            custom_cwd=target.custom_cwd,
        )
    query = WorkspaceFileRequest(
        agent_id=target.agent_id, path=target.path, mode="content", custom_cwd=target.custom_cwd
    )
    return await _forward_workspace_json(request, target.ops_url, query, WorkspaceFileResponse)


@router.get("/api/agents/{agent_id}/files/raw", response_model=None)
async def get_workspace_file_raw(
    agent_id: int,
    request: Request,
    path: Annotated[
        str, Query(description="Workspace or cwd relative path, absolute path, or file:// URL")
    ],
) -> Response:
    target = await _workspace_target(request, agent_id, path)
    if target.ops_url is not None:
        return await _stream_workspace_raw(request, target, target.ops_url)
    file, _ = await _read_locally(
        resolve_existing_workspace_file,
        gateway_state(request).gateway_process.home,
        target.agent_id,
        target.path,
        custom_cwd=target.custom_cwd,
    )
    media_type, headers = passive_headers(file.name)
    return FileResponse(path=file, stat_result=file.stat(), media_type=media_type, headers=headers)


# meiva:note
# One directory of a workspace for the viewer's tree; no `path` lists the agent's workspace root.
# The same roots and refusals as a file read apply, and the root itself may be listed.
# meiva:link gateway/agents/docs/to-user-resources.md
@router.get("/api/agents/{agent_id}/files/list", response_model=WorkspaceDirListing)
async def list_workspace_files(
    agent_id: int,
    request: Request,
    path: Annotated[
        str | None,
        Query(description="Directory: workspace or cwd relative, absolute, or file:// URL"),
    ] = None,
) -> WorkspaceDirListing:
    target = await _workspace_target(request, agent_id, path or None, allow_root=True)
    if target.ops_url is None:
        return await _read_locally(
            list_workspace_dir,
            gateway_state(request).gateway_process.home,
            target.agent_id,
            target.path,
            custom_cwd=target.custom_cwd,
        )
    query = WorkspaceFileRequest(
        agent_id=target.agent_id, path=target.path, mode="list", custom_cwd=target.custom_cwd
    )
    return await _forward_workspace_json(request, target.ops_url, query, WorkspaceDirListing)
