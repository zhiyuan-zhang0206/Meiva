# meiva:note
# A gateway on a port-0 listener that answers the routes a test scripts and records every request
# it receives (the last scripted reply of a route repeats), so a plugin's gateway reads run over a
# real HTTP exchange instead of a patched client. `clients()` hands back the explicit client set a
# context carries.
from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import urlsplit

import httpx
import pytest

from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs


@dataclass(frozen=True)
class Reply:
    body: object
    status: int = 200
    delay_seconds: float = 0.0


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    body: dict[str, Any]


@dataclass
class GatewayStub:
    replies: dict[tuple[str, str], list[Reply]] = field(default_factory=dict)
    received: list[Received] = field(default_factory=list)
    url: str = ""
    opened: list[ClientSet] = field(default_factory=list)

    def reply(self, method: str, path: str, body: object, *, status: int = 200) -> None:
        self.replies[(method, path)] = [Reply(body, status)]

    def reply_in_turn(self, method: str, path: str, bodies: list[object]) -> None:
        self.replies[(method, path)] = [Reply(body) for body in bodies]

    def reply_slowly(self, method: str, path: str, body: object, *, delay_seconds: float) -> None:
        self.replies[(method, path)] = [Reply(body, delay_seconds=delay_seconds)]

    def clients(self, *, url: str | None = None) -> ClientSet:
        built = ClientSet(
            gateway_url=self.url if url is None else url,
            gateway=lambda url: httpx.Client(base_url=url),
            factories={
                GatewayTransportInputs: lambda: GatewayTransportInputs(
                    max_retries_reader=lambda: 1,
                    retry_delay_reader=lambda: 0.0,
                    memory_deadline_reader=lambda: 15.0,
                )
            },
        )
        self.opened.append(built)
        return built

    def refused_url(self) -> str:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return f"http://127.0.0.1:{probe.getsockname()[1]}"


@pytest.fixture
def gateway() -> Iterator[GatewayStub]:
    stub = GatewayStub()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def serve(self) -> None:
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            body = cast("dict[str, Any]", json.loads(self.rfile.read(length)) if length else {})
            stub.received.append(Received(self.command, path, body))
            queue = stub.replies.get((self.command, path))
            if queue is None:
                self.send_error(404)
                return
            reply = queue[0] if len(queue) == 1 else queue.pop(0)
            time.sleep(reply.delay_seconds)
            payload = reply.body if isinstance(reply.body, str) else json.dumps(reply.body)
            data = payload.encode()
            self.send_response(reply.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            self.serve()

        def do_POST(self) -> None:
            self.serve()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub.url = f"http://127.0.0.1:{server.server_port}"
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    serving.start()
    try:
        yield stub
    finally:
        for built in stub.opened:
            built.close()
        server.shutdown()
        serving.join()
        server.server_close()
