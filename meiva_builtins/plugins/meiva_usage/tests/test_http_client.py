from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from meiva_builtins.plugins.meiva_usage.probes import (
    HttpJsonRequest,
    HttpStatusError,
    urllib_json_fetcher,
)
from meiva_builtins.plugins.meiva_usage.types import UsageProbeError


@dataclass
class _Reply:
    status: int = 200
    body: bytes = b"{}"


@pytest.fixture
def server() -> Iterator[tuple[str, _Reply]]:
    reply = _Reply()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            self.send_response(reply.status)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            _ = self.wfile.write(reply.body)

    listener = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01})
    serving.start()
    try:
        yield f"http://127.0.0.1:{listener.server_port}/usage", reply
    finally:
        listener.shutdown()
        serving.join()
        listener.server_close()


def test_json_object_body_is_returned(server: tuple[str, _Reply]) -> None:
    url, reply = server
    reply.body = b'{"used": 3, "plan": "pro"}'

    assert urllib_json_fetcher(HttpJsonRequest(url=url)) == {"used": 3, "plan": "pro"}


def test_http_status_error_carries_the_code(server: tuple[str, _Reply]) -> None:
    url, reply = server
    reply.status = 503

    with pytest.raises(HttpStatusError) as caught:
        _ = urllib_json_fetcher(HttpJsonRequest(url=url))

    assert caught.value.status_code == 503


@pytest.mark.parametrize(
    ("body", "message"),
    [(b"not json", "Invalid JSON response"), (b"[1, 2]", "Expected JSON object")],
)
def test_non_object_body_is_refused(server: tuple[str, _Reply], body: bytes, message: str) -> None:
    url, reply = server
    reply.body = body

    with pytest.raises(UsageProbeError, match=message):
        _ = urllib_json_fetcher(HttpJsonRequest(url=url))


def test_non_http_scheme_is_refused() -> None:
    with pytest.raises(UsageProbeError, match="Unsupported URL scheme: file"):
        _ = urllib_json_fetcher(HttpJsonRequest(url="file:///etc/hosts"))
