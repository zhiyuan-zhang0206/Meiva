# meiva:note
# A bounded real HTTP pool response, shared by Git and provisioning consumers.
# meiva:link base/deploy/git/tests/docs/memory-repo-tests.meiva.okf.md

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread


class PoolResponse:
    def __init__(
        self,
        body: bytes,
        *,
        head: str | None = None,
        status: int = 200,
        declared_length: str | None = None,
        release: Event | None = None,
        chunks: tuple[bytes, ...] | None = None,
    ) -> None:
        self.body = body
        self.head = head
        self.status = status
        self.declared_length = declared_length
        self.release = release
        self.chunks = chunks
        self.received = Event()
        self.headers: dict[str, str] = {}
        self.path: str | None = None


@contextlib.contextmanager
def pool_server(response: PoolResponse) -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.connection.settimeout(5)
            response.path = self.path
            response.headers = dict(self.headers)
            response.received.set()
            if response.release is not None and not response.release.wait(5):
                self.send_error(504)
                return
            self.send_response(response.status)
            if response.head is not None:
                self.send_header("X-Pool-Head", response.head)
            if response.declared_length is not None:
                self.send_header("Content-Length", response.declared_length)
            if response.chunks is not None:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                if response.chunks is None:
                    self.wfile.write(response.body)
                else:
                    for chunk in response.chunks:
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        if response.release is not None:
            response.release.set()
        server.shutdown()
        server.server_close()
        worker.join(5)
        assert not worker.is_alive(), "pool HTTP listener did not stop"
