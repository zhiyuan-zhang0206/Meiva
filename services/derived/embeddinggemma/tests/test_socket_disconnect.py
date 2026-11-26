from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event

import pytest
import uvicorn
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from services.derived.embeddinggemma.app import create_app
from services.derived.embeddinggemma.tests import fakes


class _BlockingEngine(fakes.FakeEngine):
    def __init__(self, started: asyncio.Event, release: Event) -> None:
        super().__init__()
        self._started = started
        self._release = release
        self._loop = asyncio.get_running_loop()

    def embed(
        self,
        texts: Sequence[str],
        *,
        normalize: bool,
        insert_special_tokens: bool,
        dimensions: int,
    ) -> list[list[float]]:
        if texts == ["active"]:
            _ = self._loop.call_soon_threadsafe(self._started.set)
            if not self._release.wait(10):
                raise TimeoutError("test did not release inference")
        return super().embed(
            texts,
            normalize=normalize,
            insert_special_tokens=insert_special_tokens,
            dimensions=dimensions,
        )


async def _send_embedding(
    port: int, text: str, *, observe: bool = False
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    body = json.dumps({"model": fakes.MODEL, "input": text}).encode()
    marker = "X-Waiter-Probe: yes\r\n" if observe else ""
    writer.write(
        (
            "POST /v1/embeddings HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n{marker}\r\n"
        ).encode()
        + body
    )
    await writer.drain()
    return reader, writer


@asynccontextmanager
async def _live_server(app: ASGIApp) -> AsyncGenerator[int]:
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="on"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    assert not serving.done(), "the Uvicorn server exited before it could listen"
                    await asyncio.sleep(0)
            yield port
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, 10)


@pytest.mark.asyncio
async def test_disconnected_socket_drops_queued_inference(tmp_path: Path) -> None:
    started = asyncio.Event()
    release = Event()
    waiting_for_disconnect = asyncio.Event()
    disconnected = asyncio.Event()
    engine = _BlockingEngine(started, release)
    app = create_app(fakes.server_config(tmp_path), open_engine=fakes.opener(engine))

    async def observe_waiter(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (b"x-waiter-probe", b"yes") not in scope["headers"]:
            await app(scope, receive, send)
            return

        body_read = False

        async def observe_receive() -> Message:
            nonlocal body_read
            if body_read:
                waiting_for_disconnect.set()
            message = await receive()
            if message["type"] == "http.request" and not message.get("more_body", False):
                body_read = True
            if message["type"] == "http.disconnect":
                disconnected.set()
            return message

        await app(scope, observe_receive, send)

    async with _live_server(observe_waiter) as port:
        writers: list[asyncio.StreamWriter] = []
        try:
            async with asyncio.timeout(5):
                active, writer = await _send_embedding(port, "active")
                writers.append(writer)
                await started.wait()
                _, victim = await _send_embedding(port, "disconnected", observe=True)
                writers.append(victim)
                await waiting_for_disconnect.wait()
                victim.close()
                await victim.wait_closed()
                await disconnected.wait()
                survivor, writer = await _send_embedding(port, "survivor")
                writers.append(writer)
                release.set()
                assert (await active.read()).startswith(b"HTTP/1.1 200")
                assert (await survivor.read()).startswith(b"HTTP/1.1 200")
        finally:
            release.set()
            for writer in writers:
                writer.close()
                await writer.wait_closed()
    assert [call[0] for call in engine.calls] == [["active"], ["survivor"]]
