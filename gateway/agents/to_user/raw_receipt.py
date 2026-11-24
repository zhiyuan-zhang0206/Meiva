# meiva:link gateway/agents/docs/to-user-resources.md


from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import Request, Response
from starlette.datastructures import Headers
from starlette.types import Message, Receive, Scope, Send

from base import telemetry
from gateway.http.config import gateway_clock
from gateway.http.ingress import UploadIngress, classify_ingress

__all__ = ["RawReceipt"]

_RANGE = re.compile(r"bytes=\d{0,19}-\d{0,19}")


def _ms(seconds: float) -> float:
    return round(seconds * 1000.0, 1)


def _range(value: str | None) -> str | None:
    if value is None:
        return None
    return value if _RANGE.fullmatch(value) else "other"


def _declared_length(message: Message) -> int | None:
    for name, value in Headers(raw=_raw_headers(message)).raw:
        if name == b"content-length" and value.isdigit():
            return int(value)
    return None


def _status(value: object, default: int) -> int:
    return value if isinstance(value, int) else default


def _chunk_size(body: object) -> int:
    return len(body) if isinstance(body, bytes) else 0


def _raw_headers(message: Message) -> list[tuple[bytes, bytes]]:
    headers: object = message.get("headers")
    pairs: list[tuple[bytes, bytes]] = []
    if isinstance(headers, list):
        for item in headers:  # pyright: ignore[reportUnknownVariableType]
            if isinstance(item, tuple) and len(item) == 2:  # pyright: ignore[reportUnknownArgumentType]
                name, value = item  # pyright: ignore[reportUnknownVariableType]
                if isinstance(name, bytes) and isinstance(value, bytes):
                    pairs.append((name, value))
    return pairs


@dataclass
class RawReceipt:
    now: Callable[[], float]
    started: float
    range: str | None
    conditional: bool
    ingress: UploadIngress
    remote: bool = False
    upstream_ms: float | None = None
    emitted: bool = False

    @classmethod
    def begin(cls, request: Request) -> RawReceipt:
        now = gateway_clock(request).monotonic
        return cls(
            now=now,
            started=now(),
            range=_range(request.headers.get("range")),
            conditional="if-none-match" in request.headers or "if-range" in request.headers,
            ingress=classify_ingress(request.headers),
        )

    def upstream_answered(self, asked: float) -> None:
        self.upstream_ms = _ms(self.now() - asked)

    def refused(self, status: int) -> None:
        self.emit(status=status, body_bytes=0, content_length=None, headers_at=None, ended=None)

    def wrap(self, response: Response) -> Response:
        return _Metered(response, self)

    def emit(
        self,
        *,
        status: int,
        body_bytes: int,
        content_length: int | None,
        headers_at: float | None,
        ended: float | None,
    ) -> None:
        if self.emitted:
            return
        self.emitted = True
        total = self.now() - self.started
        attributes: dict[str, object] = {
            "status": status,
            "range": self.range,
            "conditional": self.conditional,
            "remote": self.remote,
            "body_bytes": body_bytes,
            "content_length": content_length,
            "completed": ended is not None,
            "ttfb_ms": None if headers_at is None else _ms(headers_at - self.started),
            "upstream_ms": self.upstream_ms,
            "body_ms": None if headers_at is None or ended is None else _ms(ended - headers_at),
            "total_ms": _ms(total),
            "via_cloudflare": self.ingress.via_cloudflare,
            "cf_colo": self.ingress.cf_colo,
            "host": self.ingress.host,
        }
        telemetry.emit("telemetry", "workspace_file_served", attributes=attributes)


class _Metered(Response):
    def __init__(self, inner: Response, receipt: RawReceipt) -> None:
        super().__init__(status_code=inner.status_code)
        self.inner = inner
        self.receipt = receipt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        receipt = self.receipt
        status = self.inner.status_code
        declared: int | None = None
        sent = 0
        headers_at: float | None = None
        ended: float | None = None

        async def counting(message: Message) -> None:
            nonlocal status, declared, sent, headers_at, ended
            await send(message)
            if message["type"] == "http.response.start":
                status = _status(message.get("status"), status)
                declared = _declared_length(message)
                headers_at = receipt.now()
            elif message["type"] == "http.response.body":
                sent += _chunk_size(message.get("body"))
                if not message.get("more_body", False):
                    ended = receipt.now()
            elif message["type"] == "http.response.pathsend":
                sent += declared or 0
                ended = receipt.now()

        try:
            await self.inner(scope, receive, counting)
        finally:
            receipt.emit(
                status=status,
                body_bytes=sent,
                content_length=declared,
                headers_at=headers_at,
                ended=ended,
            )
        if self.background is not None:
            await self.background()
