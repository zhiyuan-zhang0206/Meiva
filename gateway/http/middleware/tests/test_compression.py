from __future__ import annotations

import gzip
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response, StreamingResponse
from httpx import ASGITransport, AsyncClient

from gateway.http.middleware.compression import install_response_compression

_ITEMS = [{"payload": "gzip me, I repeat myself"}] * 600


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/json")
    def json_answer() -> JSONResponse:
        return JSONResponse({"items": _ITEMS})

    @app.get("/small")
    def small() -> JSONResponse:
        return JSONResponse({"ok": True})

    @app.get("/archive")
    def archive() -> Response:
        return Response(b"x" * 4096, media_type="application/octet-stream")

    @app.get("/partial")
    def partial() -> Response:
        return Response(b"x" * 4096, status_code=206, media_type="text/plain")

    @app.get("/events")
    async def events() -> StreamingResponse:
        async def frames() -> AsyncIterator[bytes]:
            yield b"data: " + b"x" * 4096 + b"\n\n"

        return StreamingResponse(frames(), media_type="text/event-stream")

    install_response_compression(app)
    return app


async def _get(path: str, accept: str | None) -> tuple[dict[str, str], bytes]:
    headers = {"Accept-Encoding": "identity" if accept is None else accept}
    transport = ASGITransport(app=_app())
    async with (
        AsyncClient(transport=transport, base_url="http://t") as client,
        client.stream("GET", path, headers=headers) as response,
    ):
        raw = b"".join([chunk async for chunk in response.aiter_raw()])
    return dict(response.headers), raw


@pytest.mark.asyncio
async def test_a_json_answer_is_gzipped_for_a_client_that_accepts_it() -> None:
    headers, body = await _get("/json", "gzip")
    assert headers["content-encoding"] == "gzip"
    assert headers["vary"] == "Accept-Encoding"
    assert headers["content-length"] == str(len(body))
    assert b"gzip me" in gzip.decompress(body)
    assert len(body) < 1000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "accept"),
    [
        ("/json", None),
        ("/json", "identity"),
        ("/small", "gzip"),
        ("/archive", "gzip"),
        ("/partial", "gzip"),
        ("/events", "gzip"),
    ],
)
async def test_every_other_answer_stays_as_it_is(path: str, accept: str | None) -> None:
    headers, _ = await _get(path, accept)
    assert "content-encoding" not in headers
