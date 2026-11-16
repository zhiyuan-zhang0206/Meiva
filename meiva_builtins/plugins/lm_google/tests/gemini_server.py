# meiva:note
# A Gemini API on a port-0 listener. A test scripts the reply of `generateContent` or `decisions`
# (a response object, or a refusal with a status and an error body), points the provider at `url`
# through the model factory's `base_url` or `transport()`, and reads back the JSON requests the
# client really sent. On `/v1beta/decisions`, question keys are validated against Google's wire
# constraint `^[A-Za-z0-9_-]{1,64}$` and rejected with HTTP 400 before serving a scripted reply.
# meiva:link meiva_builtins/plugins/README.md
from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Self

import httpx
import pytest
from google.genai import types

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of

__all__ = ["GeminiServer", "gemini_server"]

_DECISION_QUESTION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _decision_validation_error(path: str, body: Mapping[str, object]) -> dict[str, object] | None:
    if not path.endswith("/decisions") and "questions" not in body:
        return None
    questions = body.get("questions")
    if not is_object_dict(questions) or not questions:
        return {
            "error": {
                "code": 400,
                "message": "* CreateDecisionRequest.questions: must be a non-empty map",
                "status": "INVALID_ARGUMENT",
            }
        }
    for qid in questions:
        if not _DECISION_QUESTION_ID.fullmatch(qid):
            return {
                "error": {
                    "code": 400,
                    "message": (
                        f"* CreateDecisionRequest.questions[{qid}]: "
                        "question IDs must be 1-64 characters matching [A-Za-z0-9_-]+"
                    ),
                    "status": "INVALID_ARGUMENT",
                }
            }
    return None


class _ServerTransport(httpx.AsyncBaseTransport):
    def __init__(self, base_url: str) -> None:
        self._url = httpx.URL(base_url)
        self._network = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(
            scheme=self._url.scheme, host=self._url.host, port=self._url.port
        )
        return await self._network.handle_async_request(request)

    async def __aenter__(self) -> Self:
        _ = await self._network.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self._network.__aexit__(exc_type, exc_value, traceback)

    async def aclose(self) -> None:
        await self._network.aclose()


@dataclass(frozen=True)
class Received:
    path: str
    body: dict[str, object]


@dataclass
class GeminiServer:
    url: str = ""
    status: int = 200
    reply: dict[str, object] = field(default_factory=dict)
    received: list[Received] = field(default_factory=list)

    def answer(self, response: types.GenerateContentResponse) -> None:
        self.status = 200
        self.reply = response.model_dump(mode="json", by_alias=True, exclude_none=True)

    def answer_decisions(
        self, answers: Mapping[str, Mapping[str, object]], *, input_tokens: int = 225
    ) -> None:
        self.status = 200
        self.reply = {
            "model": "gemini-decision-1",
            "answers": {qid: dict(answer) for qid, answer in answers.items()},
            "usage": {
                "total_tokens": input_tokens,
                "total_input_tokens": input_tokens,
                "total_output_tokens": 0,
            },
        }

    def refuse(self, status: int, message: str, code: str) -> None:
        self.status = status
        self.reply = {"error": {"code": status, "message": message, "status": code}}

    def transport(self) -> httpx.AsyncBaseTransport:
        return _ServerTransport(self.url)


@pytest.fixture
def gemini_server() -> Iterator[GeminiServer]:
    server_state = GeminiServer()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            body = object_of(json.loads, self.rfile.read(length))
            assert is_object_dict(body)
            server_state.received.append(Received(self.path, body))
            invalid = _decision_validation_error(self.path, body)
            if invalid is not None:
                status = 400
                reply: dict[str, object] = invalid
            else:
                status = server_state.status
                reply = server_state.reply
            data = json.dumps(reply).encode()
            streaming = status == 200 and "streamGenerateContent" in self.path
            if streaming:
                data = b"data: " + data + b"\r\n\r\n"
            self.send_response(status)
            self.send_header(
                "Content-Type", "text/event-stream" if streaming else "application/json"
            )
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
