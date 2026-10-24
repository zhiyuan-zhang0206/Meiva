from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from base.config import get_field, set_field
from base.host.net.tests.local_http import serve_asgi
from services.supervision.roster import build_services
from services.supervision.roster.service_spec import ServiceSpec


def _memory_search() -> ServiceSpec:
    return next(service for service in build_services() if service.session == "memory-search")


async def _search(_request: Request) -> JSONResponse:
    return JSONResponse({"hits": []})


async def _foreign_service(_request: Request) -> JSONResponse:
    return JSONResponse({"nope": 1})


def test_memory_search_roster_captures_endpoint_and_provider_until_rebuilt() -> None:
    original_uri = get_field("memory_search_uri")
    original_backend = get_field("embedding_backend")
    search = Starlette(routes=[Route("/search", _search, methods=["POST"])])
    foreign = Starlette(routes=[Route("/search", _foreign_service, methods=["POST"])])
    try:
        with serve_asgi(search) as uri, serve_asgi(foreign) as foreign_uri:
            set_field("memory_search_uri", uri)
            set_field("embedding_backend", "gemini")
            bound = _memory_search()

            set_field("memory_search_uri", foreign_uri)
            set_field("embedding_backend", "unknown-provider")

            assert bound.identity_probe is not None
            assert bound.identity_probe().alive is True
            rebuilt = _memory_search()
            assert rebuilt.identity_probe is not None
            with pytest.raises(ValueError, match="unknown embedding provider"):
                rebuilt.identity_probe()

            set_field("embedding_backend", "gemini")
            rebuilt = _memory_search()
            assert rebuilt.identity_probe is not None
            verdict = rebuilt.identity_probe()
            assert verdict.alive is False
            assert "without a hits payload" in verdict.detail
    finally:
        set_field("memory_search_uri", original_uri)
        set_field("embedding_backend", original_backend)
