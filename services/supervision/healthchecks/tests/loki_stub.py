# meiva:note
# A real HTTP listener on port 0 that answers the way the local LGTM backends do: a readiness
# endpoint, Loki's OTLP push and its `query_range`. A test scripts the push answers and the
# query behavior; `pushes` and `queries` record what arrived so a test can assert on the wire.

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, Self


@dataclass
class _Script:
    ready: tuple[int, bytes] = (200, b"")
    push_answers: list[tuple[int, bytes]] = field(default_factory=lambda: [(204, b"")])
    query_mode: Literal["echo", "empty", "drop"] = "echo"
    pushes: list[dict[str, Any]] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    last_marker: str = ""


class BackendStub:
    def __init__(self) -> None:
        script = _Script()
        self.script = script

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _answer(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/ready":
                    self._answer(*script.ready)
                    return
                script.queries.append(self.path)
                if script.query_mode == "drop":
                    self.connection.close()
                    return
                streams = (
                    [{"values": [["1", script.last_marker]]}] if script.query_mode == "echo" else []
                )
                self._answer(200, json.dumps({"data": {"result": streams}}).encode())

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                payload: dict[str, Any] = json.loads(self.rfile.read(length))
                record = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
                script.last_marker = record["body"]["stringValue"]
                script.pushes.append(
                    {
                        "path": self.path,
                        "org": self.headers.get("X-Scope-OrgID"),
                        "content_type": self.headers.get("Content-Type"),
                        "payload": payload,
                    }
                )
                answers = script.push_answers
                self._answer(*(answers.pop(0) if len(answers) > 1 else answers[0]))

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
