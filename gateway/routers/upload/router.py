# meiva:link gateway/routers/docs/upload-batches.meiva.okf.md

from __future__ import annotations

import asyncio
import hashlib
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Path, Request, Response
from fastapi.responses import FileResponse

from base.agents.upload_delivery import source
from base.agents.upload_delivery.models import (
    BATCH_ID_PATTERN,
    ObjectReadRequest,
    UploadDeliveryConflictError,
)
from base.agents.upload_delivery.paths import (
    agent_upload_dir,
    delivered_upload_headers,
    render_safe_headers,
    resolve_upload_path,
)
from base.api_contracts.agent_contract import AgentNotFound
from base.cluster.machine import gateway_auth_headers
from base.cluster.machines import MachineGatewayUrlMissing, MachineNotRegistered, lookup
from base.db import agent_exists

router = APIRouter()


async def _fetch_delivered_object(
    client: httpx.AsyncClient,
    url: str,
    query: ObjectReadRequest,
    headers: dict[str, str],
    *,
    expected_size: int,
    expected_sha256: str,
) -> bytes:
    async with client.stream("POST", url, json=query.model_dump(), headers=headers) as response:
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail="upload not found")
        if response.status_code != 200:
            raise HTTPException(status_code=502, detail="delivered upload could not be read")
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > expected_size:
                raise HTTPException(status_code=404, detail="upload not found")
    data = bytes(content)
    if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise HTTPException(status_code=404, detail="upload not found")
    return data


@router.get("/api/agents/{agent_id}/uploads/{batch_id}/{ordinal}", response_model=None)
async def serve_delivered_upload(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    batch_id: Annotated[str, Path(pattern=BATCH_ID_PATTERN)],
    ordinal: Annotated[int, Path(ge=0, lt=1000)],
    request: Request,
) -> Response:
    try:
        target, _directory, item = await asyncio.to_thread(
            source.accepted_object, request.app.state.db_pool, agent_id, batch_id, ordinal
        )
    except (AgentNotFound, UploadDeliveryConflictError) as exc:
        raise HTTPException(status_code=404, detail="upload not found") from exc
    access = request.app.state.gateway_access
    query = ObjectReadRequest(agent_id=agent_id, batch_id=batch_id, ordinal=ordinal)
    try:
        ops_url = await asyncio.to_thread(lookup, request.app.state.db, target.machine)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=3), trust_env=False
        ) as client:
            content = await _fetch_delivered_object(
                client,
                f"{ops_url.rstrip('/')}/upload-delivery/object",
                query,
                gateway_auth_headers(access),
                expected_size=item.size,
                expected_sha256=item.sha256,
            )
    except (httpx.HTTPError, MachineGatewayUrlMissing, MachineNotRegistered) as exc:
        raise HTTPException(status_code=503, detail="The agent's machine is unavailable") from exc
    return Response(
        content,
        media_type=item.content_type,
        headers=delivered_upload_headers(item.name, item.content_type, item.sha256),
    )


@router.get("/api/agents/{agent_id}/uploads/{filename}")
def serve_upload(agent_id: int, filename: str, request: Request) -> FileResponse:
    with request.app.state.db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise AgentNotFound(agent_id)
    try:
        path = resolve_upload_path(
            agent_upload_dir(
                agent_id, create=False, root=request.app.state.gateway_config.upload_staging_root
            ),
            filename,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"upload {filename!r} not found")
    return FileResponse(path, headers=render_safe_headers(filename))
