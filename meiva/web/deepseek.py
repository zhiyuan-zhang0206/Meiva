# meiva:note
# The DeepSeek Search and Fetch tools: build each request, send it paced by the batch's rate
# limiter, scan what comes back for injection patterns, and return the hits with the findings, or
# the page.
# meiva:link meiva/web/docs/providers.meiva.okf.md

from __future__ import annotations

from meiva.sdk_surface.pure.media.finding_plan import Scanned
from meiva.web import deepseek_wire, transport, wire
from meiva.web.deps import FetchDeps, SearchDeps
from meiva.web.errors import FetchError, SearchError

__all__ = ["read_page", "search_one"]


def search_one(
    query: str, count: int, deps: SearchDeps, pacer: transport.RateLimiter
) -> Scanned[list[wire.Hit]]:
    config = deps.deepseek
    data = transport.send_json(
        deepseek_wire.deepseek_search_request(config, query, count),
        timeout_s=config.timeout_s,
        label="DeepSeek Search",
        error_type=SearchError,
        provider_json=True,
        pacer=pacer,
    )
    hits = deepseek_wire.deepseek_search_hits(data)
    return Scanned(hits, wire.hit_findings(hits))


def read_page(url: str, max_chars: int, deps: FetchDeps, pacer: transport.RateLimiter) -> wire.Page:
    config = deps.deepseek
    data = transport.send_json(
        deepseek_wire.deepseek_fetch_request(config, url),
        timeout_s=config.timeout_s,
        label="DeepSeek Fetch",
        error_type=FetchError,
        provider_json=True,
        pacer=pacer,
    )
    return deepseek_wire.deepseek_page(data, url, max_chars)
