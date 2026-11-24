"""Settings-free HTTP transport shared by normal and bootstrap daemon modes.

Callers validate their own unit/image and provide explicit bind/auth policy.
This transport never initializes Settings, registers a unit, or opens a database.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import BinaryIO

from opentelemetry.trace import SpanKind, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from base.host.net.auth import verify_bearer_digest

RouteHandler = Callable[[bytes], Awaitable[tuple[int, bytes, str]]]


# meiva:link base/daemon/docs/http-transport.md
@dataclass(frozen=True, slots=True)
class FileBody:
    handle: BinaryIO
    length: int


# meiva:note
# What the transport serves: a route answers status, body and content type, and may add response
# headers (a byte-range answer's `Content-Range`) as a fourth element. The body is bytes held in
# memory or a `FileBody` streamed from disk. Every `RouteHandler` is one.
RouteAnswer = (
    tuple[int, bytes | FileBody, str] | tuple[int, bytes | FileBody, str, Mapping[str, str]]
)
ServedRoute = Callable[[bytes], Awaitable[RouteAnswer]]
_MAX_BODY_BYTES = 64 * 1024
_log = logging.getLogger("base.daemon.http_transport")

# meiva:link base/daemon/docs/http-transport.md
_HEAD_TIMEOUT_S = 2.0
_BODY_TIMEOUT_S = 10.0
STREAM_CHUNK_BYTES = 64 * 1024
_WRITE_BLOCKED_TIMEOUT_S = 30.0
_POLL_S = 1.0
_REASONS = {
    200: "OK",
    206: "Partial Content",
    400: "Bad Request",
    401: "Unauthorized",
    404: "Not Found",
    413: "Payload Too Large",
    416: "Range Not Satisfiable",
    422: "Unprocessable Content",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


# meiva:link base/daemon/docs/http-transport.md
@dataclass(frozen=True, slots=True)
class KeepAlive:
    idle_seconds: float = 30.0
    max_requests: int = 100

    def __post_init__(self) -> None:
        if self.idle_seconds <= 0 or self.max_requests <= 0:
            raise ValueError("keep-alive bounds must be positive")


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    body: bytes | FileBody
    content_type: str
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    drained: bool = False

    @property
    def length(self) -> int:
        return self.body.length if isinstance(self.body, FileBody) else len(self.body)


class _BadFramingError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    path: str
    version: str
    header_lines: list[bytes]
    content_length: int

    @classmethod
    def parse(cls, head: bytes) -> _Request:
        header_lines = head.split(b"\r\n")
        parts = header_lines[0].decode("ascii", errors="replace").split(" ")
        return cls(
            parts[0],
            parts[1] if len(parts) > 1 else "",
            parts[2] if len(parts) > 2 else "",
            header_lines,
            _content_length(header_lines),
        )

    @property
    def wants_keep_alive(self) -> bool:
        if self.version != "HTTP/1.1":
            return False
        tokens = (_header_value(self.header_lines, b"connection") or "").lower().split(",")
        return "close" not in (token.strip() for token in tokens)


@dataclass(frozen=True, slots=True)
class _Served:
    routes: Mapping[tuple[str, str], ServedRoute]
    health_response: Callable[[], tuple[int, bytes]]
    auth_digests: frozenset[str] | None
    body_limits: Mapping[tuple[str, str], int]
    body_slots: Mapping[tuple[str, str], asyncio.Semaphore]
    tracer: Tracer | None
    keep_alive: KeepAlive | None
    serving: Callable[[], bool]


def _header_value(header_lines: list[bytes], name: bytes) -> str | None:
    """Extract a header value by (case-insensitive) name from raw request lines.
    `header_lines[0]` is the request line, so the scan starts at index 1."""
    prefix = name.lower() + b":"
    for h in header_lines[1:]:
        if h.lower().startswith(prefix):
            return h.split(b":", 1)[1].strip().decode("latin-1")
    return None


def _content_length(header_lines: list[bytes]) -> int:
    """Declared body size: 0 when absent.

    A request that cannot be framed (a line ending other than CRLF, which two parsers may split
    differently; a repeated or non-numeric `Content-Length`; any `Transfer-Encoding`, which this
    transport does not decode) raises `_BadFramingError`: guessing "no body" would leave the
    body's bytes in the stream to be read as the next request, which on a reused connection is
    request smuggling."""
    if any(b"\n" in line or b"\r" in line for line in header_lines):
        raise _BadFramingError("a bare line feed or carriage return in the header")
    declared: list[bytes] = []
    for h in header_lines[1:]:
        lowered = h.lower()
        if lowered.startswith(b"transfer-encoding:"):
            raise _BadFramingError("Transfer-Encoding is not supported")
        if lowered.startswith(b"content-length:"):
            declared.append(h.split(b":", 1)[1].strip())
    if not declared:
        return 0
    if len(declared) > 1 or not (declared[0].isascii() and declared[0].isdigit()):
        raise _BadFramingError("Content-Length must be one decimal number")
    return int(declared[0])


def _response(
    status: int, body: bytes | FileBody, content_type: str, headers: Mapping[str, str] | None = None
) -> _Response:
    return _Response(status, body, content_type, headers or {})


def _json_response(status: int, payload: Mapping[str, str]) -> _Response:
    return _response(status, json.dumps(payload).encode(), "application/json")


def _head(response: _Response, *, keep_alive: bool) -> bytes:
    extra = "".join(f"{name}: {value}\r\n" for name, value in response.headers.items())
    connection = "keep-alive" if keep_alive else "close"
    reason = _REASONS.get(response.status, "Unknown")
    return (
        f"HTTP/1.1 {response.status} {reason}\r\n".encode("ascii")
        + f"Content-Type: {response.content_type}\r\n".encode("ascii")
        + f"Content-Length: {response.length}\r\n".encode("ascii")
        + extra.encode("ascii")
        + f"Connection: {connection}\r\n\r\n".encode("ascii")
    )


async def _routed_response(
    reader: asyncio.StreamReader, request: _Request, served: _Served
) -> _Response:
    """Read the capped POST body and answer the request from health, the routes, or 404."""
    route = (request.method, request.path)
    if route == ("GET", "/healthz"):
        status, healthz_body = served.health_response()
        return _response(status, healthz_body, "application/json")
    if route not in served.routes:
        return _response(404, b"", "text/plain")
    if served.auth_digests is not None and not verify_bearer_digest(
        _header_value(request.header_lines, b"authorization"), served.auth_digests
    ):
        return _json_response(401, {"error": "unauthorized"})
    limit = served.body_limits.get(route, _MAX_BODY_BYTES)
    if request.content_length > limit:
        message = f"body too large: {request.content_length} > {limit}"
        return _json_response(400, {"error": message})
    if served.tracer is None:
        return await _dispatch_with_slot(reader, request.content_length, route, served)
    carrier = {
        key.decode("ascii"): value
        for key in (b"traceparent", b"tracestate")
        if (value := _header_value(request.header_lines, key)) is not None
    }
    with served.tracer.start_as_current_span(
        f"{request.method} {request.path}",
        context=TraceContextTextMapPropagator().extract(carrier),
        kind=SpanKind.SERVER,
        attributes={"http.request.method": request.method, "http.route": request.path},
        record_exception=False,
        set_status_on_exception=False,
    ) as span:
        response = await _dispatch_with_slot(reader, request.content_length, route, served)
        span.set_attribute("http.response.status_code", response.status)
        span.set_attribute("http.response.body.size", response.length)
        if response.status >= 500:
            span.set_status(StatusCode.ERROR)
        return response


async def _dispatch_with_slot(
    reader: asyncio.StreamReader, length: int, route: tuple[str, str], served: _Served
) -> _Response:
    gate = served.body_slots.get(route)
    if gate is None:
        return await _read_and_dispatch(reader, length, served.routes[route], route)
    if gate.locked():
        return _json_response(503, {"error": "route is busy"})
    async with gate:
        return await _read_and_dispatch(reader, length, served.routes[route], route)


async def _read_and_dispatch(
    reader: asyncio.StreamReader, length: int, handler: ServedRoute, route: tuple[str, str]
) -> _Response:
    body_bytes = b""
    if length > 0:
        body_bytes = await asyncio.wait_for(reader.readexactly(length), timeout=_BODY_TIMEOUT_S)
    try:
        status, body, content_type, *headers = await handler(body_bytes)
        answer = _response(status, body, content_type, headers[0] if headers else None)
    except Exception as exc:
        _log.exception("[http_transport] route %s %s handler raised", *route)
        answer = _json_response(500, {"error": type(exc).__name__})
    return _Response(answer.status, answer.body, answer.content_type, answer.headers, drained=True)


async def _write(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(data)
    await asyncio.wait_for(writer.drain(), timeout=_WRITE_BLOCKED_TIMEOUT_S)


async def _send(writer: asyncio.StreamWriter, response: _Response, *, keep_alive: bool) -> bool:
    head = _head(response, keep_alive=keep_alive)
    body = response.body
    if isinstance(body, bytes):
        await _write(writer, head + body)
        return True
    if body.length == 0:
        await _write(writer, head)
        return True
    pending = head
    remaining = body.length
    while remaining > 0:
        chunk = await asyncio.to_thread(body.handle.read, min(STREAM_CHUNK_BYTES, remaining))
        if not chunk:
            _log.warning("[http_transport] file body ended %d bytes short of its length", remaining)
            return False
        await _write(writer, pending + chunk)
        pending = b""
        remaining -= len(chunk)
    return True


async def _read_head(
    reader: asyncio.StreamReader, *, timeout_s: float, serving: Callable[[], bool]
) -> bytes | None:
    with contextlib.suppress(
        TimeoutError, asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError
    ):
        for _ in range(max(1, math.ceil(timeout_s / _POLL_S))):
            if not serving():
                return None
            try:
                return await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), timeout=min(timeout_s, _POLL_S)
                )
            except TimeoutError:
                continue
    return None


def _always() -> bool:
    return True


def _reusable(request: _Request, response: _Response, served: _Served, *, nth: int) -> bool:
    keep_alive = served.keep_alive
    return (
        keep_alive is not None
        and nth + 1 < keep_alive.max_requests
        and served.serving()
        and request.wants_keep_alive
        and (response.drained or request.content_length == 0)
    )


async def _exchange(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    head: bytes,
    served: _Served,
    *,
    nth: int,
) -> bool:
    try:
        request = _Request.parse(head)
        response = await _routed_response(reader, request, served)
        reusable = _reusable(request, response, served, nth=nth)
    except _BadFramingError as exc:
        response = _json_response(400, {"error": str(exc)})
        reusable = False
    try:
        return await _send(writer, response, keep_alive=reusable) and reusable
    finally:
        if isinstance(response.body, FileBody):
            response.body.handle.close()


async def _serve_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, served: _Served
) -> None:
    keep_alive = served.keep_alive
    for nth in range(1 if keep_alive is None else keep_alive.max_requests):
        waiting_for_next = nth > 0 and keep_alive is not None
        head = await _read_head(
            reader,
            timeout_s=keep_alive.idle_seconds
            if waiting_for_next and keep_alive
            else _HEAD_TIMEOUT_S,
            serving=served.serving if waiting_for_next else _always,
        )
        if head is None or not await _exchange(reader, writer, head, served, nth=nth):
            return


async def start_daemon_http(
    *,
    host: str,
    port: int,
    health_response: Callable[[], tuple[int, bytes]],
    extra_routes: Mapping[tuple[str, str], ServedRoute] | None = None,
    auth_digests: frozenset[str] | None = None,
    body_limits: Mapping[tuple[str, str], int] | None = None,
    body_slots: Mapping[tuple[str, str], asyncio.Semaphore] | None = None,
    tracer: Tracer | None = None,
    keep_alive: KeepAlive | None = None,
) -> asyncio.Server:
    """Bind only the explicitly supplied routes and health response."""
    server: asyncio.Server | None = None
    served = _Served(
        routes=dict(extra_routes) if extra_routes else {},
        health_response=health_response,
        auth_digests=auth_digests,
        body_limits=dict(body_limits or {}),
        body_slots=dict(body_slots or {}),
        tracer=tracer,
        keep_alive=keep_alive,
        serving=lambda: server is None or server.is_serving(),
    )

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        clean = False
        try:
            # Client disconnected early / did not send valid HTTP — silent drop;
            # the caller (healthcheck / SDK) takes the outer fallback. The
            # finally still closes our writer.
            with contextlib.suppress(
                TimeoutError,
                asyncio.IncompleteReadError,
                ConnectionError,
                asyncio.LimitOverrunError,
            ):
                await _serve_connection(reader, writer, served)
                clean = True
        finally:
            if not clean:
                writer.transport.abort()
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    # Default bind 127.0.0.1 — healthcheck / local RPC; no LAN port. The
    # agent-runner ops server overrides host=0.0.0.0 so the gateway can
    # reach its POST /ops route over the private network (same posture as the gateway).
    server = await asyncio.start_server(handle, host=host, port=port)
    return server
