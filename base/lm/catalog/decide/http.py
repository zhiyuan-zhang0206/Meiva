"""The HTTP step shared by decision providers: one authenticated JSON POST, no retries.

Retrying is the caller's decision (``decide()`` is a pre-flight check or a gate, and a caller
that wants a second attempt can call it again), so a transport failure or a non-success status
surfaces as ``DecisionRequestError`` after exactly one request.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from base.lm.catalog.decide.types import (
    DecisionBuildContext,
    not_json_failure,
    require_json_object,
    require_success,
    transport_failure,
)


# meiva:note
# POST ``payload`` as JSON with a bearer key; return the decoded JSON object.
async def post_json(
    ctx: DecisionBuildContext, *, url: str, api_key: str, payload: Mapping[str, object]
) -> dict[str, object]:
    import httpx  # deferred: the provider contract loads on the exec-child boot path

    raw_transport: Any = ctx.transport
    transport: httpx.AsyncBaseTransport | None = raw_transport
    try:
        async with httpx.AsyncClient(transport=transport, timeout=ctx.timeout) as client:
            response = await client.post(
                url, json=payload, headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.HTTPError as exc:
        raise transport_failure(ctx.model, url, exc) from exc
    require_success(
        ctx.model,
        url,
        status_code=response.status_code,
        is_success=response.is_success,
        text=response.text,
    )
    try:
        body: object = response.json()
    except ValueError as exc:
        raise not_json_failure(ctx.model, url) from exc
    return require_json_object(ctx.model, url, body)
