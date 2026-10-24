# meiva:note
# The hosts a relay delivers into, as real listeners on this machine: a Codex app server on a
# unix socket (JSON-RPC over a websocket) and an Antigravity language server on a loopback
# port (Connect JSON over HTTP).
#
# Each one answers the way the host does and keeps what it was sent. The Codex server's answers
# are scripted at construction, one per `turn/start` in arrival order with the last one
# repeating, so a test can have the host accept the start message and refuse the push without
# a switch to flip while the relay runs.

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from websockets.sync.server import ServerConnection, unix_serve

__all__ = [
    "AntigravityPost",
    "AntigravityServerLog",
    "AppServerLog",
    "CodexHome",
    "antigravity_server",
    "codex_app_server",
    "codex_home",
    "short_socket_path",
]


# meiva:note
# What a Codex app server was asked, in arrival order: each turn's thread and text.
@dataclass
class AppServerLog:
    endpoint: str
    turns: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    @property
    def texts(self) -> list[str]:
        return [text for _, text in self.turns]


@dataclass(frozen=True)
class AntigravityPost:
    path: str
    csrf_token: str | None
    body: dict[str, Any]


# meiva:note
# What an Antigravity language server was sent, and where to reach it.
@dataclass
class AntigravityServerLog:
    address: str
    posts: list[AntigravityPost] = field(default_factory=list[AntigravityPost])

    @property
    def texts(self) -> list[str]:
        return [post.body["content"] for post in self.posts]


# meiva:note
# A bind path short enough for macOS's 104-character AF_UNIX limit; pytest's `tmp_path` is too
# deep. The directory is removed when the block ends.
@contextmanager
def short_socket_path() -> Generator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="cxas-", dir="/tmp"))
    try:
        yield directory / "codex.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


# meiva:note
# Where the local Codex daemon would listen: a CODEX_HOME whose `app-server-control` directory
# exists, and the socket path `codex` resolves inside it. No daemon runs until a test starts
# `codex_app_server` on `socket`.
@dataclass(frozen=True)
class CodexHome:
    home: str
    socket: Path


@pytest.fixture
def codex_home() -> Iterator[CodexHome]:
    with short_socket_path() as anchor:
        control = anchor.parent / "app-server-control"
        control.mkdir()
        yield CodexHome(str(anchor.parent), control / "app-server-control.sock")


# meiva:note
# A Codex app server on `path` whose n-th `turn/start` is accepted when `accepts[n]` is true and
# refused otherwise (the last entry covers every later turn).
@contextmanager
def codex_app_server(path: Path, accepts: Sequence[bool]) -> Generator[AppServerLog]:
    log = AppServerLog(endpoint=f"unix://{path}")

    def answer(conn: ServerConnection) -> None:
        for raw in conn:
            request = json.loads(raw)
            if request.get("method") == "initialize":
                conn.send(json.dumps({"id": request["id"], "result": {"userAgent": "scripted"}}))
            elif request.get("method") == "turn/start":
                params = request["params"]
                log.turns.append((params["threadId"], params["input"][0]["text"]))
                accepted = accepts[min(len(log.turns), len(accepts)) - 1]
                if accepted:
                    reply = {"id": request["id"], "result": {"turn": {"id": "turn-1"}}}
                else:
                    error = {"code": -32603, "message": "ActiveTurnNotSteerable"}
                    reply = {"id": request["id"], "error": error}
                conn.send(json.dumps(reply))

    server = unix_serve(answer, path=str(path), open_timeout=5, close_timeout=1, compression=None)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield log
    finally:
        server.shutdown()
        thread.join(timeout=5)
        path.unlink(missing_ok=True)


# meiva:note
# An Antigravity language server on a free loopback port that accepts every SendAgentMessage.
@contextmanager
def antigravity_server() -> Generator[AntigravityServerLog]:
    log = AntigravityServerLog(address="")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            token = self.headers.get("x-codeium-csrf-token")
            log.posts.append(AntigravityPost(self.path, token, body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    log.address = f"127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield log
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
