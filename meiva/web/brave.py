# meiva:note
# Search Brave for one query: build the request, send it, and return the hits.

from __future__ import annotations

from base.host.net.resilience import Policy
from meiva.web import transport, wire
from meiva.web.deps import SearchDeps
from meiva.web.errors import SearchError
from meiva.web.policy import WEB_READ_POLICY

__all__ = ["search_one"]


def search_one(
    query: str, count: int, deps: SearchDeps, *, policy: Policy = WEB_READ_POLICY
) -> list[wire.Hit]:
    request = wire.brave_request(deps.brave, query, count)
    data = transport.send_json(
        request,
        timeout_s=deps.brave.timeout_s,
        label="Brave Search",
        error_type=SearchError,
        provider_json=False,
        pacer=None,
        rng=deps.rng,
        policy=policy,
    )
    return wire.brave_hits(data)
