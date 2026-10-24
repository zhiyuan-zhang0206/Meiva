# meiva:note
# Search Brave for one query: build the request, send it, scan every title and snippet for
# injection patterns, and return the hits.

from __future__ import annotations

from meiva import security
from meiva.web import transport, wire
from meiva.web.deps import SearchDeps
from meiva.web.errors import SearchError

__all__ = ["search_one"]


def search_one(query: str, count: int, deps: SearchDeps) -> list[wire.Hit]:
    request = wire.brave_request(deps.brave, query, count)
    data = transport.send_json(
        request,
        timeout_s=deps.brave.timeout_s,
        label="Brave Search",
        error_type=SearchError,
        provider_json=False,
        pacer=None,
    )
    hits = wire.brave_hits(data)
    for hit in hits:
        security.scan_to(deps.delivery, hit.title, "web.search")
        security.scan_to(deps.delivery, hit.snippet, "web.search")
    return hits
