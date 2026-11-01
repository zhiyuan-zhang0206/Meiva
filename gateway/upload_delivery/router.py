# meiva:link gateway/upload_delivery/docs/delivered-uploads/delivered-uploads.meiva.okf.md

from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Path,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from base.agents.upload_delivery import source, staging
from base.agents.upload_delivery.models import (
    BATCH_ID_PATTERN,
    FILE_ID_PATTERN,
    Acceptance,
    DeliveryStatus,
    StagedFile,
    UploadDeliveryConflictError,
    UploadQuotaExceededError,
)
from base.agents.upload_delivery.paths import render_safe_headers
from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.http.auth.request_principal import (
    PRINCIPAL_SCOPE,
    SCOPE_HEADER,
    AuthPrincipal,
    principal_key,
)
from gateway.http.config import gateway_clock, gateway_config

router = APIRouter()


class UploadSendRequest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    file_ids: list[Annotated[str, Field(pattern=FILE_ID_PATTERN)]] = Field(
        min_length=1, max_length=1000
    )
    text: str = Field(default="", max_length=1_048_576)


def operation_key(
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
    idempotency_scope: str = Header(alias=SCOPE_HEADER),
) -> str:
    principal = getattr(request.state, "auth_principal", None)
    if idempotency_scope != PRINCIPAL_SCOPE or not isinstance(principal, AuthPrincipal):
        raise HTTPException(422, "uploads require verified principal-v1 scope")
    return principal_key(principal, request.method, request.url.path, idempotency_key)


@router.post("/api/keyed/v1/agents/{agent_id}/uploads/staged", status_code=201)
async def stage_upload(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    request: Request,
    key: Annotated[str, Depends(operation_key)],
    file: Annotated[UploadFile, File()],
) -> StagedFile:
    config = gateway_config(request)
    raw_name = file.filename or "untitled"
    contents = await file.read(config.max_upload_bytes + 1)
    if len(contents) > config.max_upload_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"upload {raw_name!r} exceeds the {config.max_upload_bytes:,}-byte per-file limit",
        )
    file_id = UUID(int=config.rng.getrandbits(128), version=4).hex
    try:
        return await request.app.state.upload_recovery.native(
            staging.stage,
            request.app.state.db_pool,
            key,
            agent_id,
            raw_name=raw_name,
            data=contents,
            content_type=file.content_type or "application/octet-stream",
            file_id=file_id,
            upload_root=config.upload_staging_root,
            local_machine=request.app.state.machine.name(),
            home=request.app.state.gateway_process.home,
            max_bytes=config.max_agent_upload_bytes,
            max_files=config.max_agent_upload_files,
        )
    except UploadDeliveryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except UploadQuotaExceededError as exc:
        raise HTTPException(413, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, "invalid staged upload") from exc


@router.delete("/api/keyed/v1/agents/{agent_id}/uploads/staged/{file_id}", status_code=204)
async def discard_staged_upload(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    file_id: Annotated[str, Path(pattern=FILE_ID_PATTERN)],
    request: Request,
) -> Response:
    try:
        await request.app.state.upload_recovery.native(
            staging.discard,
            request.app.state.db_pool,
            agent_id,
            file_id,
            local_machine=request.app.state.machine.name(),
            home=request.app.state.gateway_process.home,
        )
    except UploadDeliveryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(status_code=204)


@router.post("/api/keyed/v1/agents/{agent_id}/uploads", status_code=202)
async def upload(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    body: UploadSendRequest,
    request: Request,
    key: Annotated[str, Depends(operation_key)],
) -> Acceptance:
    config = gateway_config(request)
    batch_id = UUID(int=config.rng.getrandbits(128), version=4).hex
    sent_at = gateway_clock(request).now()
    try:
        accepted = await request.app.state.upload_recovery.native(
            source.send,
            request.app.state.db_pool,
            key,
            agent_id,
            file_ids=body.file_ids,
            text=body.text,
            provenance=request_inbound_provenance(request),
            sent_at=sent_at,
            batch_id=batch_id,
            local_machine=request.app.state.machine.name(),
            home=request.app.state.gateway_process.home,
            upload_root=config.upload_staging_root,
        )
    except UploadDeliveryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    except UploadQuotaExceededError as exc:
        raise HTTPException(413, str(exc)) from exc
    except ValidationError as exc:
        raise HTTPException(422, "invalid immutable upload manifest") from exc
    request.app.state.upload_recovery.nudge()
    return accepted


@router.get("/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}")
async def status(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    request: Request,
    batch_id: Annotated[str, Path(pattern=BATCH_ID_PATTERN)],
) -> DeliveryStatus:
    return await request.app.state.upload_recovery.native(
        source.status, request.app.state.db_pool, agent_id, batch_id
    )


@router.get("/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}/objects/{ordinal}")
async def object_file(
    agent_id: Annotated[int, Path(gt=0, lt=2**63)],
    request: Request,
    batch_id: Annotated[str, Path(pattern=BATCH_ID_PATTERN)],
    ordinal: Annotated[int, Path(ge=0)],
) -> FileResponse:
    try:
        path, item = await request.app.state.upload_recovery.native(
            source.served_object,
            request.app.state.db_pool,
            agent_id,
            batch_id,
            ordinal,
            local_machine=request.app.state.machine.name(),
            home=request.app.state.gateway_process.home,
        )
    except UploadDeliveryConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    headers = render_safe_headers(item.name)
    headers.pop("Content-Disposition", None)
    return FileResponse(
        path,
        media_type=item.content_type,
        filename=item.name,
        headers=headers,
    )
