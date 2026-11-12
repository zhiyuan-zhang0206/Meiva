# meiva:link base/lm/docs/decision-models.md

from __future__ import annotations

from collections.abc import Mapping

from base.host.boundary.untyped import object_of
from base.lm.catalog.decide.types import (
    DecisionBuildContext,
    not_json_failure,
    require_json_object,
    require_success,
    transport_failure,
)


# meiva:note
# POST once with the provider authentication headers or the existing bearer-key form.
async def post_json(
    ctx: DecisionBuildContext,
    *,
    url: str,
    api_key: str | None,
    payload: Mapping[str, object],
    headers: Mapping[str, str] | None = None,
) -> dict[str, object]:
    # meiva:note
    # The lightweight provider contract loads on the exec-child boot path.
    import httpx

    if api_key is not None and headers is not None:
        raise ValueError("choose bearer api_key or explicit authentication headers, not both")
    request_headers = (
        dict(headers)
        if headers is not None
        else ({"Authorization": f"Bearer {api_key}"} if api_key is not None else {})
    )
    transport = ctx.transport
    if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
        raise TypeError(
            f"a decision transport must be an httpx.AsyncBaseTransport, got {type(transport)!r}"
        )
    try:
        async with httpx.AsyncClient(transport=transport, timeout=ctx.timeout) as client:
            response = await client.post(
                url,
                json=payload,
                headers=request_headers,
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
        body = object_of(response.json)
    except ValueError as exc:
        raise not_json_failure(ctx.model, url) from exc
    return require_json_object(ctx.model, url, body)
