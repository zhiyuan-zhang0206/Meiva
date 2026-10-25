# meiva:note
# A finite loopback provider; the real SDK builds and sends the model request.

from __future__ import annotations

import json
import threading
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Self

import httpx
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from base.lm.provider_api import BuildContext


class ProviderWire:
    def __init__(self, status: int, reply: bytes) -> None:
        self.requests: list[bytes] = []
        self.arrived = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.build_efforts: list[str] = []
        self.client = httpx.Client(timeout=5)
        wire = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                wire.requests.append(self.rfile.read(int(self.headers["Content-Length"])))
                wire.arrived.set()
                if not wire.release.wait(5):
                    self.send_error(504, "the test did not release its provider response")
                    return
                with suppress(BrokenPipeError, ConnectionResetError):
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(reply)))
                    self.end_headers()
                    self.wfile.write(reply)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )

    def build(self, ctx: BuildContext) -> ChatOpenAI:
        self.build_efforts.append(ctx.resolved_effort)
        if ctx.resolved_effort != "high":
            raise ValueError("the local provider expects the agent's pinned high effort")
        return ChatOpenAI(
            model=ctx.model,
            api_key=SecretStr(ctx.keys.require("DEEPSEEK_API_KEY")),
            base_url=f"http://127.0.0.1:{self.server.server_port}/v1",
            http_client=self.client,
            max_retries=0,
            reasoning_effort="high",
            timeout=5,
        )

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.client.close()
        if self.thread.is_alive():
            raise RuntimeError("the local provider did not stop")


def completion(text: str) -> bytes:
    return json.dumps(
        {
            "id": "history-local-response",
            "object": "chat.completion",
            "created": 1,
            "model": "deepseek-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        }
    ).encode()
