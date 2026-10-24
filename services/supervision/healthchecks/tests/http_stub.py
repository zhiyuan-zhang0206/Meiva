# meiva:note
# A real HTTP listener on port 0 that answers every request with one scripted status and body.
# With `raw`, it writes those bytes instead of an HTTP response (a malformed answer). `requests` records each request as `(method, path, body)`. A test reads the URL, asserts on
# the verdict a probe reaches and, where the request is the behavior, on what arrived.

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self


class HttpStub:
    def __init__(
        self, *, status: int = 200, body: bytes = b"", hang: bool = False, raw: bytes | None = None
    ) -> None:
        self.status = status
        self.body = body
        self.requests: list[tuple[str, str, bytes]] = []
        stub = self
        released = threading.Event()
        self._released = released

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                stub.requests.append((self.command, self.path, self.rfile.read(length)))
                if hang:
                    released.wait(timeout=30)
                    return
                if raw is not None:
                    self.wfile.write(raw)
                    return
                self.send_response(stub.status)
                self.send_header("Content-Length", str(len(stub.body)))
                self.end_headers()
                self.wfile.write(stub.body)

            def do_GET(self) -> None:
                self._serve()

            def do_POST(self) -> None:
                self._serve()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.port: int = self._server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._released.set()
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
