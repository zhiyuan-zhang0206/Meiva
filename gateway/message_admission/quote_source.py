# meiva:link gateway/message_admission/README.md

import asyncio
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException, Request
from langchain_core.messages import BaseMessage
from pydantic import BaseModel, ValidationError

from base.agents.epochs.chain import usable_epochs
from base.agents.epochs.model import HarnessEpoch
from base.agents.epochs.native_slice import NativeEpochSlice, moment_of, native_slice_of
from base.agents.epochs.read import read_epochs
from base.agents.history.checkpoint import CheckpointReadError
from base.agents.history.checkpoint_segment import checkpoint_quote_exists
from base.agents.history.native_source import (
    has_native_quote_block,
    native_message_identity,
    native_source_stamps,
)
from base.agents.history.transcript_timeline import record_quote_coordinates
from base.agents.messages.quote import Quote
from base.api_contracts.agent_contract import AgentStatus
from base.db.rows import query_one
from gateway.http.config import gateway_config, gateway_rpc, gateway_state
from gateway.message_admission.legacy_quote import validate_legacy_quote
from ops.agents import get_agent_status
from ops.cluster.rpc import ClusterOpFailed, ClusterOpUnreachable
from ops.rpc_schemas import (
    HarnessTimelineReadRequest,
    HarnessTimelineWindow,
    RecordQuoteEvidence,
    RecordUnreadable,
)


class _Placement(BaseModel):
    machine: str
    harness: str | None


@dataclass(frozen=True, slots=True)
class _AdmissionContext:
    machine: str
    harness: str
    epochs: list[HarnessEpoch]


def _admission_context(request: Request, agent_id: int) -> _AdmissionContext:
    with gateway_state(request).db_pool.connection() as conn:
        placement = query_one(
            conn, _Placement, "SELECT machine, harness FROM agents_meta WHERE id = %s", (agent_id,)
        )
        if placement is None:
            raise HTTPException(404, "Agent not found")
        harness = placement.harness or "meiva"
        epochs = usable_epochs(read_epochs(conn, agent_id), harness)
    return _AdmissionContext(placement.machine, harness, epochs)


def _contains_native_source(
    messages: list[BaseMessage], quote: Quote, epoch: NativeEpochSlice | None
) -> bool:
    block = quote.source_block_idx
    if block is None:
        return False
    stamps = native_source_stamps(messages) if epoch is not None else {}
    for index, message in enumerate(messages):
        identity = native_message_identity(message)
        if quote.source_message_id is not None and identity.message_id != quote.source_message_id:
            continue
        if quote.source_inbound_id is not None and identity.inbound_id != quote.source_inbound_id:
            continue
        if epoch is not None and not epoch.holds(stamps.get(index)):
            continue
        if has_native_quote_block(message, block):
            return True
    return False


def _native_source_exists(
    request: Request, agent_id: int, quote: Quote, context: _AdmissionContext
) -> bool:
    epoch = native_slice_of(context.epochs, context.epochs[-1].seq) if context.epochs else None
    return checkpoint_quote_exists(
        gateway_state(request).db,
        agent_id,
        lambda messages: _contains_native_source(messages, quote, epoch),
    )


def _record_epoch_scope(context: _AdmissionContext) -> tuple[int | None, datetime | None]:
    current = context.epochs[-1] if context.epochs else None
    earlier = [epoch for epoch in context.epochs[:-1] if epoch.harness == context.harness]
    floor = current.source_start if current is not None else None
    if floor is None:
        floor = max(
            (epoch.source_end for epoch in earlier if epoch.source_end is not None), default=None
        )
    return floor, current.started_at if current and earlier and floor is None else None


async def _record_evidence(
    request: Request, machine: str, read: HarnessTimelineReadRequest
) -> RecordQuoteEvidence:
    try:
        result = await gateway_rpc(request).dispatch_to_machine(
            gateway_state(request).db,
            target_machine=machine,
            kind="harness_timeline_read",
            payload=read.wire_payload(),
            timeout_s=gateway_config(request).timeline_read_timeout_seconds,
            retries=0,
        )
    except ClusterOpUnreachable as exc:
        raise HTTPException(503, "Quote source machine is unavailable") from exc
    except ClusterOpFailed as exc:
        raise HTTPException(503, "Quote source identity reader is unavailable") from exc
    try:
        window = HarnessTimelineWindow.model_validate(result)
    except ValidationError as exc:
        raise HTTPException(502, "Invalid quote source evidence") from exc
    if not window.quote_applied or window.unreadable in (
        RecordUnreadable.FILE_MISSING,
        RecordUnreadable.UNDECODABLE,
    ):
        raise HTTPException(503, "Quote source identity reader is unavailable")
    source = window.quote_evidence
    if source is None or (source.identity, source.inbound_id) != (
        read.quote_identity,
        read.quote_inbound_id,
    ):
        raise HTTPException(422, "Quote source does not exist in this conversation")
    return source


async def _validate_record_identity(
    request: Request,
    agent_id: int,
    quote: Quote,
    identity: str,
    context: _AdmissionContext,
) -> None:
    coordinates = record_quote_coordinates(identity)
    if (
        coordinates is None
        or quote.source_block_idx is not None
        or (quote.source_inbound_id is not None and quote.source_inbound_id >= 2**63)
    ):
        raise HTTPException(422, "Invalid quote source identity")
    floor, since = _record_epoch_scope(context)
    if floor is not None and coordinates.offset < floor:
        raise HTTPException(422, "Quotes from an earlier harness session are not supported")
    read = HarnessTimelineReadRequest(
        agent_id=agent_id,
        harness=context.harness,
        limit=1,
        explicit_visibility=False,
        delivery_mode=None,
        session_id=coordinates.session,
        quote_identity=identity,
        quote_inbound_id=quote.source_inbound_id,
    )
    source = await _record_evidence(request, context.machine, read)
    if since is not None:
        stamp = moment_of(source.created_at)
        if stamp is None or stamp < since:
            raise HTTPException(422, "Quotes from an earlier harness session are not supported")
    if (
        source.activity_pending
        and await asyncio.to_thread(get_agent_status, gateway_state(request).db, agent_id)
        is AgentStatus.RUNNING
    ):
        raise HTTPException(422, "Quote source is still running")


async def validate_quote_source(request: Request, agent_id: int, quote: Quote | None) -> None:
    if quote is None:
        return
    if quote.source_agent_id != agent_id:
        raise HTTPException(422, "Quotes must come from the current conversation")
    context = await asyncio.to_thread(_admission_context, request, agent_id)
    if (
        context.harness == "meiva"
        and quote.source_block_idx is not None
        and (quote.source_message_id is not None or quote.source_inbound_id is not None)
    ):
        try:
            exists = await asyncio.to_thread(
                _native_source_exists, request, agent_id, quote, context
            )
        except CheckpointReadError as exc:
            raise HTTPException(503, "Quote source history is unavailable") from exc
        if not exists:
            raise HTTPException(422, "Quote source does not exist in this conversation")
        return
    if (
        context.harness != "meiva"
        and (identity := quote.source_message_id)
        and identity.startswith("record:v1:")
    ):
        await _validate_record_identity(request, agent_id, quote, identity, context)
        return
    await validate_legacy_quote(
        request, agent_id, quote, harness=context.harness, machine=context.machine
    )
