# meiva:note
# Search Brave for one query: build the request, send it, scan every title and snippet for
# injection patterns, and return the hits with the findings.

from __future__ import annotations

from base.host.net.resilience import Policy
from meiva.sdk_surface.pure.media.finding_plan import Scanned
from meiva.web import transport, wire
from meiva.web.deps import SearchDeps
from meiva.web.errors import SearchError
from meiva.web.policy import WEB_READ_POLICY

__all__ = ["search_one"]


def search_one(
    query: str, count: int, deps: SearchDeps, *, policy: Policy = WEB_READ_POLICY
) -> Scanned[list[wire.Hit]]:
    request = wire.brave_request(deps.brave, query, count)
    data = transport.send_json(
        request,
        timeout_s=deps.brave.timeout_s,
        label="Brave Search",
        error_type=SearchError,
        provider_json=False,
        pacer=None,
        policy=policy,
    )
    hits = wire.brave_hits(data)
    return Scanned(hits, wire.hit_findings(hits))
