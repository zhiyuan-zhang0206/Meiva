from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from base.agents.history.checkpoint import CheckpointReadError
from base.agents.history.timeline import reading_target
from base.agents.history.timeline_item import ReadingWindow
from base.api_contracts.reading import (
    MAX_READING_LOCATOR_LENGTH,
    NativeReadingLocator,
    ReadingDirection,
    ReadingSourceUnavailableError,
    ReadingUnavailableReason,
    RecordReadingLocator,
    RecordReadingSource,
    decode_reading_locator,
    encode_reading_source,
)
from base.db import agent_exists
from gateway.agents.eval_guard import deny_isolated_result_read
from gateway.agents.history.configuration import request_timeline_read
from gateway.agents.history.native_reading_window import native_reading_window
from gateway.agents.history.record_timeline import fetch_record_window
from gateway.http.config import gateway_state
from gateway.http.middleware.error_envelope import error_response
from gateway.message_admission.harness import agent_harness
from ops.rpc_schemas import HarnessTimelineWindow

router = APIRouter()


def _harness(request: Request, agent_id: int) -> str:
    with gateway_state(request).db_pool.connection() as conn:
        if not agent_exists(conn, agent_id):
            raise HTTPException(404, "Agent not found")
        return agent_harness(conn, agent_id)


def _selector(
    request: Request, anchor: str | None, before: str | None, after: str | None
) -> tuple[ReadingDirection, str]:
    choices = [
        (direction, value)
        for direction, value in (
            (ReadingDirection.ANCHOR, anchor),
            (ReadingDirection.BEFORE, before),
            (ReadingDirection.AFTER, after),
        )
        if value is not None
    ]
    if len(choices) != 1 or any(
        len(request.query_params.getlist(name)) > 1 for name in ("anchor", "before", "after")
    ):
        raise HTTPException(422, "A reading window requires exactly one selector")
    return choices[0]


def _record_result(
    window: HarnessTimelineWindow, locator: RecordReadingLocator, direction: ReadingDirection
) -> ReadingWindow:
    if window.reading_unavailable is not None:
        raise ReadingSourceUnavailableError(
            window.reading_unavailable, "The verified reading source is unavailable"
        )
    source = RecordReadingSource.model_validate(locator.model_dump(exclude={"offset", "block"}))
    if not window.reading_applied or window.reading_source != encode_reading_source(source):
        raise HTTPException(503, "The source did not confirm this reading selector")
    for item in window.items:
        bound = _record_locator(item.reading_locator or "", source)
        if bound.item_id() != item.item_id:
            raise HTTPException(503, "The source returned a different reading coordinate")
    _record_cursors(window, locator, direction, source)
    return ReadingWindow(
        items=window.items,
        reading_source=window.reading_source,
        anchor_item_id=window.anchor_item_id,
        older_cursor=window.older_cursor,
        newer_cursor=window.newer_cursor,
    )


def _record_cursors(
    window: HarnessTimelineWindow,
    locator: RecordReadingLocator,
    direction: ReadingDirection,
    source: RecordReadingSource,
) -> None:
    ids = {item.item_id for item in window.items}
    for cursor in (window.older_cursor, window.newer_cursor):
        if cursor is not None and _record_locator(cursor, source).item_id() not in ids:
            raise HTTPException(503, "The source cursor has no verified window member")
    continuation = (
        window.older_cursor if direction is ReadingDirection.BEFORE else window.newer_cursor
    )
    if direction is not ReadingDirection.ANCHOR and continuation is not None:
        next_offset = _record_locator(continuation, source).offset
        progressing = (
            next_offset < locator.offset
            if direction is ReadingDirection.BEFORE
            else next_offset > locator.offset
        )
        if not progressing:
            raise HTTPException(503, "The source reading cursor did not progress")


def _record_locator(encoded: str, source: RecordReadingSource) -> RecordReadingLocator:
    try:
        bound = decode_reading_locator(encoded)
    except ValueError as exc:
        raise HTTPException(503, "The source returned unqualified reading metadata") from exc
    if not isinstance(bound, RecordReadingLocator) or (
        bound.agent_id,
        bound.harness,
        bound.session_id,
    ) != (source.agent_id, source.harness, source.session_id):
        raise HTTPException(503, "The source returned a different reading identity")
    return bound


@router.get(
    "/api/agents/{agent_id}/timeline/window",
    dependencies=[Depends(deny_isolated_result_read)],
    response_model=ReadingWindow,
)
async def get_reading_window(
    request: Request,
    agent_id: int,
    anchor: str | None = Query(default=None, max_length=MAX_READING_LOCATOR_LENGTH),
    before: str | None = Query(default=None, max_length=MAX_READING_LOCATOR_LENGTH),
    after: str | None = Query(default=None, max_length=MAX_READING_LOCATOR_LENGTH),
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> ReadingWindow | JSONResponse:
    direction, encoded = _selector(request, anchor, before, after)
    try:
        locator = decode_reading_locator(encoded)
    except ValueError as exc:
        raise HTTPException(422, "Invalid reading locator") from exc
    harness = await asyncio.to_thread(_harness, request, agent_id)
    if locator.agent_id != agent_id:
        return _unavailable(request, ReadingUnavailableReason.SOURCE)
    read = request_timeline_read(request)
    count = read.default_limit if limit is None else limit
    try:
        if isinstance(locator, RecordReadingLocator) and locator.harness == harness:
            result = _record_result(
                await fetch_record_window(
                    request,
                    agent_id,
                    harness,
                    limit=count,
                    before=None,
                    reading_locator=encoded,
                    reading_direction=direction,
                ),
                locator,
                direction,
            )
        elif isinstance(locator, NativeReadingLocator) and harness == "meiva":
            result = await asyncio.to_thread(
                native_reading_window,
                gateway_state(request).db,
                locator,
                direction,
                count,
                read.compact_history,
                read.rendering,
            )
        else:
            return _unavailable(request, ReadingUnavailableReason.SOURCE)
        if direction is ReadingDirection.ANCHOR:
            try:
                target = reading_target(result.items, locator)
            except ReadingSourceUnavailableError as exc:
                raise HTTPException(
                    503, "The source did not return the requested canonical target"
                ) from exc
            if result.anchor_item_id != result.items[target].item_id:
                raise HTTPException(503, "The source did not confirm the canonical reading target")
        return result
    except ReadingSourceUnavailableError as exc:
        return _unavailable(request, exc.reason)
    except CheckpointReadError as exc:
        raise HTTPException(503, "Checkpoint history unavailable") from exc


def _unavailable(request: Request, reason: ReadingUnavailableReason) -> JSONResponse:
    return error_response(
        request,
        code="reading_source_unavailable",
        status=410,
        detail="The reading source could not be verified",
        retryable=False,
        extensions={"reading_reason": reason.value},
    )
