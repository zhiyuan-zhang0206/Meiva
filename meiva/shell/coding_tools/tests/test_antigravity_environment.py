from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from base.agents.impersonation.host_transport import AntigravityServer
from meiva.shell.coding_tools import antigravity

_CONVERSATION = "6f1d0c2e-3b7a-4f55-9a52-0c8e4d7b9a10"
_CSRF = "csrf-from-env"


# meiva:note
# A real Connect JSON-RPC server on a loopback port that answers every POST with `{}` and records
# the path, the CSRF header and the JSON body of each request it received.
@contextmanager
def _language_server() -> Generator[tuple[str, list[tuple[str, str | None, dict[str, Any]]]]]:
    received: list[tuple[str, str | None, dict[str, Any]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            received.append((self.path, self.headers.get("x-codeium-csrf-token"), body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{httpd.server_address[1]}", received
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
        httpd.server_close()


def _send(server: AntigravityServer) -> int:
    return antigravity.send(_CONVERSATION, "hi", antigravity_server=server)


def _status(server: AntigravityServer) -> int:
    return antigravity.status(_CONVERSATION, antigravity_server=server)


def _stop(server: AntigravityServer) -> int:
    return antigravity.stop(_CONVERSATION, antigravity_server=server)


@pytest.mark.parametrize(
    ("call", "method", "payload"),
    [
        (_send, "SendAgentMessage", {"recipient": _CONVERSATION, "content": "hi"}),
        (_status, "GetConversationMetadata", {"conversationId": _CONVERSATION}),
        (_stop, "ForceStopCascadeTree", {"conversationId": _CONVERSATION}),
    ],
)
def test_a_command_reaches_the_language_server_it_was_given(
    call: Callable[[AntigravityServer], int], method: str, payload: dict[str, str]
) -> None:
    with _language_server() as (address, received):
        code = call(AntigravityServer(address=address, csrf_token=_CSRF))

    assert code == 0
    assert received == [(f"/exa.language_server_pb.LanguageServerService/{method}", _CSRF, payload)]
