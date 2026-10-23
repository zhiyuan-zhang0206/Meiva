from __future__ import annotations

import json
import socketserver
import subprocess
from collections.abc import Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def wire_response(response: bytes) -> Generator[str]:
    class Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            self.request.recv(8192)
            self.request.sendall(response)

    with (
        socketserver.TCPServer(("127.0.0.1", 0), Handler) as server,
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        serving = executor.submit(server.serve_forever, poll_interval=0.01)
        try:
            yield f"http://127.0.0.1:{server.server_address[1]}/healthz"
        finally:
            server.shutdown()
            serving.result(timeout=5)


def json_response(payload: Mapping[str, object]) -> bytes:
    body = json.dumps(dict(payload)).encode()
    return (
        f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
        + body
    )


def commit_source(root: Path, message: str) -> str:
    def git(*args: str) -> str:
        # meiva:note
        # Only fixture-authored Git argument lists run in this temporary repository.
        return subprocess.run(  # noqa: S603
            [
                "git",
                "-c",
                "user.name=Daemon test",
                "-c",
                "user.email=daemon@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()

    if not (root / ".git").exists():
        git("init")
    git("commit", "--allow-empty", "-m", message)
    return git("rev-parse", "HEAD")
