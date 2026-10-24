# meiva:note
# A Gemini API on a port-0 listener. A test scripts the reply of `generateContent` (a response
# object, or a refusal with a status and an error body), points the provider at `url` through the
# model factory's `base_url`, and reads back the JSON requests the SDK really sent, so the exchange
# runs through the genai client instead of a replaced method.
# meiva:link meiva_builtins/plugins/docs/plugins.meiva.okf.md
from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from google.genai import types

__all__ = ["GeminiServer", "gemini_server"]


@dataclass(frozen=True)
class Received:
    path: str
    body: dict[str, Any]


@dataclass
class GeminiServer:
    url: str = ""
    status: int = 200
    reply: dict[str, Any] = field(default_factory=dict)
    received: list[Received] = field(default_factory=list)

    def answer(self, response: types.GenerateContentResponse) -> None:
        self.status = 200
        self.reply = response.model_dump(mode="json", by_alias=True, exclude_none=True)

    def refuse(self, status: int, message: str, code: str) -> None:
        self.status = status
        self.reply = {"error": {"code": status, "message": message, "status": code}}


@pytest.fixture
def gemini_server() -> Iterator[GeminiServer]:
    server_state = GeminiServer()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            server_state.received.append(Received(self.path, json.loads(self.rfile.read(length))))
            data = json.dumps(server_state.reply).encode()
            self.send_response(server_state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    listener = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_state.url = f"http://127.0.0.1:{listener.server_port}"
    serving = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01})
    serving.start()
    try:
        yield server_state
    finally:
        listener.shutdown()
        serving.join()
        listener.server_close()
