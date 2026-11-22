from __future__ import annotations

import hmac
import json
from collections.abc import Callable

from starlette.types import ASGIApp, Receive, Scope, Send

from .win32_ffi import is_object_seq


def _extract_auth_header(raw_headers: object) -> bytes:
    if not is_object_seq(raw_headers):
        return b""
    for pair in raw_headers:
        if (
            is_object_seq(pair)
            and len(pair) == 2
            and isinstance(pair[0], bytes)
            and isinstance(pair[1], bytes)
            and pair[0].lower() == b"authorization"
        ):
            return pair[1]
    return b""


class BearerAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        token: str,
        *,
        health_payload: Callable[[], dict[str, object]],
    ) -> None:
        self._app = app
        self._expected = f"Bearer {token}".encode()
        self._health_payload = health_payload

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") == "http":
            path = str(scope.get("path") or "")
            if path == "/healthz":
                payload = json.dumps(self._health_payload(), ensure_ascii=False).encode("utf-8")
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(payload)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": payload})
                return

            auth = _extract_auth_header(scope.get("headers"))
            if not hmac.compare_digest(auth, self._expected):
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", b"Bearer"),
                            (b"content-length", str(len(body)).encode("ascii")),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

        await self._app(scope, receive, send)
