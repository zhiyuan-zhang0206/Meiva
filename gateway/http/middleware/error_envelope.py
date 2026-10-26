"""Shared construction of typed gateway error responses."""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from base import telemetry
from base.api_contracts.agent_contract import ErrorReason
from gateway.http.middleware.correlation import correlation
from gateway.schemas import ErrorEnvelope


def _trace_id(request: Request, *, fresh: str | None) -> str:
    """Return the active OTel trace id or this request's generated correlation id."""
    active, _span_id = telemetry.capture_trace_ids()
    chosen = correlation(
        active=active, assigned=getattr(request.state, "trace_id", None), fresh=fresh
    )
    if chosen.assign:
        request.state.trace_id = chosen.trace_id
    return chosen.trace_id


# meiva:note
# The middleware that binds a fallback correlation id before any other gateway middleware can
# reject a request. `rng` draws the id of a request that has no active trace. Every error response
# of the request reads the id bound here, so this must be the outermost middleware.
def request_trace_middleware(
    rng: random.Random,
) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    async def bind_trace_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        fresh = UUID(int=rng.getrandbits(128), version=4).hex
        request.state.trace_id = _trace_id(request, fresh=fresh)
        return await call_next(request)

    return bind_trace_id


def error_response(
    request: Request,
    *,
    code: str,
    status: int,
    detail: str,
    retryable: bool,
    reason: ErrorReason | None = None,
    extensions: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """Create the one JSON error shape used by all gateway API error paths."""
    content = ErrorEnvelope(
        code=code,
        status=status,
        detail=detail,
        retryable=retryable,
        trace_id=_trace_id(request, fresh=None),
        reason=reason,
    ).model_dump(mode="json", exclude_none=True)
    if extensions:
        overlap = set(content) & set(extensions)
        if overlap:
            raise ValueError(f"error envelope extensions duplicate fields: {sorted(overlap)}")
        content.update(extensions)
    return JSONResponse(
        status_code=status,
        content=content,
        headers=headers,
        media_type="application/problem+json",
    )
