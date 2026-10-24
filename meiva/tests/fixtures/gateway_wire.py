# meiva:note
# The gateway wire for the SDK client tests. A port-0 HTTP listener answers the routes a test
# scripts (the last reply of a route repeats) and remembers what reached it; the SDK's own httpx
# client reaches it through a transport that records every outgoing request as a value and can
# raise a scripted transport error either before the request leaves (the pre-send family) or after
# the listener really received it (the reply is lost). Tests assert on the recorded requests, on
# what the listener received and on the result the SDK returned or raised, never on calls to a
# stand-in client. `GatewayWire.context` is the explicit context a base transport function takes;
# `bound_wire` also binds it as the process context for the `meiva.gateway_client` facade.
from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.context.identity import AgentIdentity
from meiva.sdk_surface import process_context

CLIENT_TIMEOUT_SECONDS = 7.5
HELD_REPLY_BOUND_SECONDS = 30.0


@dataclass(frozen=True)
class Reply:
    status: int
    body: object
    release: threading.Event | None = None


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    headers: httpx.Headers
    body: Any
    query: str


@dataclass
class GatewayStub:
    replies: dict[tuple[str, str], list[Reply]] = field(default_factory=dict)
    received: list[Received] = field(default_factory=list)
    arrived: threading.Event = field(default_factory=threading.Event)
    url: str = ""

    def reply(self, method: str, path: str, body: object = None, *, status: int = 200) -> None:
        self.replies[(method, path)] = [Reply(status, body)]

    def hold(
        self,
        method: str,
        path: str,
        release: threading.Event,
        body: object = None,
        *,
        status: int = 200,
    ) -> None:
        self.replies[(method, path)] = [Reply(status, body, release)]

    def reply_in_turn(self, method: str, path: str, replies: list[Reply]) -> None:
        self.replies[(method, path)] = list(replies)


@dataclass(frozen=True)
class Sent:
    method: str
    path: str
    headers: httpx.Headers
    body: Any
    timeout: dict[str, float | None]
    at: float

    @property
    def intent(self) -> tuple[str, str, Any, str | None]:
        return (self.method, self.path, self.body, self.headers.get("idempotency-key"))


@dataclass(frozen=True)
class Fault:
    error: type[httpx.TransportError]
    message: str = "scripted fault"
    delivered: bool = False


class WireTransport(httpx.HTTPTransport):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Sent] = []
        self.faults: list[Fault] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(
            Sent(
                method=request.method,
                path=request.url.path,
                headers=request.headers,
                body=json.loads(request.content) if request.content else None,
                timeout=dict(request.extensions["timeout"]),
                at=time.monotonic(),
            )
        )
        if self.faults:
            fault = self.faults.pop(0)
            if fault.delivered:
                super().handle_request(request).close()
            raise fault.error(fault.message, request=request)
        return super().handle_request(request)


@dataclass(frozen=True)
class GatewayWire:
    stub: GatewayStub
    transport: WireTransport
    context: MeivaContext

    @classmethod
    def open(cls, stub: GatewayStub, **options: Any) -> GatewayWire:
        transport = WireTransport()
        return cls(stub, transport, wire_context(stub, transport, **options))

    def __enter__(self) -> GatewayWire:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.context.clients.close()
        self.transport.close()

    @property
    def sent(self) -> list[Sent]:
        return self.transport.sent

    def fail(
        self, error: type[httpx.TransportError], *, delivered: bool = False, count: int = 1
    ) -> None:
        self.transport.faults.extend([Fault(error, delivered=delivered)] * count)


def wire_context(
    stub: GatewayStub,
    transport: WireTransport,
    *,
    retries: int = 3,
    retry_delay_seconds: float = 0.0,
    memory_deadline_seconds: float = 15.0,
    agent_id: int | None = None,
) -> MeivaContext:
    return MeivaContext(
        identity=None if agent_id is None else AgentIdentity(agent_id=agent_id, owns_loop=True),
        clients=ClientSet(
            gateway_url=stub.url,
            gateway=lambda url: httpx.Client(
                base_url=url, timeout=CLIENT_TIMEOUT_SECONDS, transport=transport
            ),
            factories={
                GatewayTransportInputs: lambda: GatewayTransportInputs(
                    max_retries_reader=lambda: retries,
                    retry_delay_reader=lambda: retry_delay_seconds,
                    memory_deadline_reader=lambda: memory_deadline_seconds,
                )
            },
        ),
    )


@pytest.fixture
def gateway_stub() -> Iterator[GatewayStub]:
    stub = GatewayStub()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def serve(self) -> None:
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            stub.received.append(
                Received(
                    self.command,
                    path,
                    httpx.Headers(dict(self.headers.items())),
                    body,
                    urlsplit(self.path).query,
                )
            )
            stub.arrived.set()
            queue = stub.replies.get((self.command, path))
            if queue is None:
                self.send_error(404)
                return
            reply = queue[0] if len(queue) == 1 else queue.pop(0)
            if reply.release is not None:
                reply.release.wait(HELD_REPLY_BOUND_SECONDS)
            payload = reply.body if isinstance(reply.body, str) else json.dumps(reply.body)
            data = payload.encode()
            with suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        def do_GET(self) -> None:
            self.serve()

        def do_POST(self) -> None:
            self.serve()

        def do_PATCH(self) -> None:
            self.serve()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    stub.url = f"http://127.0.0.1:{server.server_port}"
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    serving.start()
    try:
        yield stub
    finally:
        server.shutdown()
        serving.join()
        server.server_close()


@pytest.fixture
def wire(gateway_stub: GatewayStub) -> Iterator[GatewayWire]:
    with GatewayWire.open(gateway_stub) as opened:
        yield opened


@pytest.fixture
def bound_wire(wire: GatewayWire) -> Iterator[GatewayWire]:
    previous = process_context.bound_context()
    process_context.bind_context(wire.context)
    try:
        yield wire
    finally:
        if previous is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(previous)
