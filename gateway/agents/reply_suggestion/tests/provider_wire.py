# meiva:note
# A finite local HTTP provider, not a chat-model implementation. The real Jev and LangChain
# adapters serialize requests and parse replies. The listener persists the bytes it received;
# tests inspect that receipt rather than replacing or spying on model calls.
from __future__ import annotations

import json
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from types import TracebackType
from typing import Any, Self

import httpx

DEFAULT_REPLY = '{"suggestions": ["Yes, go ahead.", "Wait, explain the plan first."]}'


@dataclass(frozen=True)
class GateReply:
    probability: float = 0.9
    status: int = 200
    body: str | None = None


@dataclass(frozen=True)
class DraftReply:
    text: str = DEFAULT_REPLY
    status: str = "completed"
    http_status: int = 200
    wait_for_disconnect: bool = False
    error_message: str | None = None


# meiva:note
# The provider contract already accepts an HTTP transport. This adapter changes only the network
# destination, then delegates socket I/O to httpx's real transport; it never answers a request.
class LocalTransport:
    def __init__(self, base_url: str) -> None:
        self._url = httpx.URL(base_url)
        self._network = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(
            scheme=self._url.scheme, host=self._url.host, port=self._url.port
        )
        return await self._network.handle_async_request(request)

    async def __aenter__(self) -> Self:
        await self._network.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._network.__aexit__(exc_type, exc, traceback)


class ProviderWire:
    def __init__(self, directory: Path, gate: tuple[GateReply, ...], draft: DraftReply) -> None:
        if not gate:
            raise ValueError("a gate script needs at least one reply")
        self.directory = directory
        directory.mkdir()
        self.started, self.disconnected = Event(), Event()
        self._gate = gate
        self._draft = draft
        self._ordinal = 0
        wire = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 2

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                ordinal = wire._ordinal
                wire._ordinal += 1
                (directory / f"{ordinal}.json").write_text(
                    json.dumps({"path": self.path, "body": json.loads(body)})
                )
                if self.path == "/v1/systemone":
                    replies = wire.gate_receipts()
                    reply = gate[min(len(replies) - 1, len(gate) - 1)]
                    data = (
                        reply.body
                        if reply.body is not None
                        else json.dumps(
                            {
                                "model": "jev-1.13.0",
                                "answers": {
                                    "useful_reply": {"type": "noul", "noul": reply.probability}
                                },
                                "usage": {"input_tokens": 80, "output_tokens": 0},
                            }
                        )
                    )
                    self.answer(reply.status, data)
                elif self.path == "/responses":
                    wire.started.set()
                    if draft.wait_for_disconnect:
                        self.connection.settimeout(2)
                        with suppress(TimeoutError):
                            try:
                                if self.connection.recv(1) == b"":
                                    wire.disconnected.set()
                            except ConnectionResetError:
                                wire.disconnected.set()
                        return
                    self.answer(draft.http_status, json.dumps(wire.response()))
                else:
                    self.answer(404, "{}")

            def answer(self, status: int, body: str) -> None:
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = False
        self._thread = Thread(
            target=partial(self._server.serve_forever, poll_interval=0.01), daemon=True
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        assert not self._thread.is_alive()

    def gate_receipts(self) -> list[dict[str, Any]]:
        return self._receipts("/v1/systemone")

    def draft_receipts(self) -> list[dict[str, Any]]:
        return self._receipts("/responses")

    def _receipts(self, path: str) -> list[dict[str, Any]]:
        records = [json.loads(p.read_text()) for p in sorted(self.directory.glob("*.json"))]
        return [record["body"] for record in records if record["path"] == path]

    def response(self) -> dict[str, object]:
        return {
            "id": "resp_test",
            "object": "response",
            "created_at": 1,
            "status": self._draft.status,
            "error": (
                {"code": "server_error", "message": self._draft.error_message}
                if self._draft.error_message is not None
                else None
            ),
            "model": "deepseek-flash",
            "output": [
                {
                    "type": "message",
                    "id": "msg_test",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": self._draft.text, "annotations": []}
                    ],
                }
            ],
            "usage": {
                "input_tokens": 120,
                "output_tokens": 14,
                "total_tokens": 134,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        }
