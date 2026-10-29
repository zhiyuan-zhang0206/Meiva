# meiva:link base/lm/docs/decision-models.meiva.okf.md

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
# POST once; omit authentication when the provider has no configured credential.
async def post_json(
    ctx: DecisionBuildContext, *, url: str, api_key: str | None, payload: Mapping[str, object]
) -> dict[str, object]:
    # meiva:note
    # The lightweight provider contract loads on the exec-child boot path.
    import httpx

    raw_transport: Any = ctx.transport
    transport: httpx.AsyncBaseTransport | None = raw_transport
    try:
        async with httpx.AsyncClient(transport=transport, timeout=ctx.timeout) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"} if api_key is not None else {},
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
