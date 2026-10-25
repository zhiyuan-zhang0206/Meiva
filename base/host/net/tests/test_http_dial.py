"""`base/host/net/http_dial.py` — pin an IPv4-literal target to AF_INET, bypassing
`getaddrinfo` (and any DNS64/NAT64 synthesis for it) entirely.

These tests run against a real local TCP server (no network mocking of the
target itself) so `_PinnedIPv4Backend.connect_tcp` exercises a genuine
socket connect/read/write round trip; what's faked is `socket.getaddrinfo`
itself — patched to raise if called, which is the actual property under
test: the pinned path must never reach it for a literal host.
"""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator

import httpx
import pytest

from base.host.net.http_dial import get as dial_get
from base.host.net.http_dial import post as dial_post
from base.host.net.http_dial import put as dial_put
from base.host.net.http_dial import transport_for_url


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass  # silence per-request stderr logging; signature matches the base class's

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def do_GET(self) -> None:
        self._respond()

    def do_POST(self) -> None:
        self._respond()

    def do_PUT(self) -> None:
        self._respond()


@pytest.fixture
def echo_server() -> Iterator[str]:
    """A real HTTP server on loopback (an IPv4 literal), yielding its base URL."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join()


@pytest.fixture
def forbid_getaddrinfo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything under it calls socket.getaddrinfo — the
    property a pinned dial must guarantee for a literal target."""

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("socket.getaddrinfo must not be called for an IPv4-literal dial")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)


class TestTransportForUrl:
    def test_hostname_gets_no_transport(self) -> None:
        # None -> httpx's own default transport, unchanged behavior.
        assert transport_for_url("http://example.com/") is None

    def test_ipv4_literal_gets_pinned_transport(self) -> None:
        from base.host.net.http_dial import PinnedIPv4Transport

        t = transport_for_url("http://10.0.0.72:8000/api/bootstrap")
        assert isinstance(t, PinnedIPv4Transport)

    def test_ipv6_literal_gets_no_transport(self) -> None:
        # Only the IPv4-literal-gets-v6-synthesized failure mode is in scope.
        assert transport_for_url("http://[::1]:8000/") is None


class TestPinnedDialNeverResolves:
    """The actual guarantee: get/post/put against an IPv4 literal never call
    getaddrinfo, and the round trip still works end to end."""

    def test_get(self, echo_server: str, forbid_getaddrinfo: None) -> None:
        resp = dial_get(f"{echo_server}/", timeout=3)
        assert resp.status_code == 200
        assert resp.content == b"ok"

    def test_post(self, echo_server: str, forbid_getaddrinfo: None) -> None:
        resp = dial_post(f"{echo_server}/", json={"a": 1}, timeout=3)
        assert resp.status_code == 200

    def test_put(self, echo_server: str, forbid_getaddrinfo: None) -> None:
        resp = dial_put(f"{echo_server}/", json={"a": 1}, timeout=3)
        assert resp.status_code == 200


class TestHostnameTargetsUseStandardResolution:
    def test_get_resolves_hostname(self, echo_server: str) -> None:
        resp = dial_get(f"{echo_server.replace('127.0.0.1', 'localhost')}/", timeout=3)
        assert resp.status_code == 200
        assert resp.content == b"ok"

    def test_post_resolves_hostname(self, echo_server: str) -> None:
        resp = dial_post(
            f"{echo_server.replace('127.0.0.1', 'localhost')}/", json={"a": 1}, timeout=3
        )
        assert resp.status_code == 200
        assert resp.content == b"ok"


class TestExceptionMapping:
    """Errors from the pinned path must surface as the same httpx exception
    types the stock backend raises — existing callers (base/host/env/bootstrap.py,
    meiva/gateway_client/__init__.py) catch httpx.ConnectError / httpx.ConnectTimeout /
    httpx.TransportError specifically, not raw httpcore/socket errors."""

    def test_connection_refused_raises_httpx_connect_error(self) -> None:
        with pytest.raises(httpx.ConnectError):
            # Port 1 on loopback: nothing listens there, refused immediately.
            dial_get("http://127.0.0.1:1/", timeout=3)
