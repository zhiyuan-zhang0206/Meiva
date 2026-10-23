# meiva:link cli/docs/operator-surfaces/public-client-wire.meiva.okf.md
from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

import pytest

from base.host.proc import run_bounded

WIRE_CREDENTIAL = "cli-wire-fixture"
_CHILD = Path(__file__).with_name("child_public_commands.py")
_ROUTES = {
    "GET": r"/api/(agents|schedules(?:/\d+(?:/(?:logs|runs))?)?)",
    "POST": r"/api/(agents/\d+/(?:messages|restart|resurrect|terminate|cancel|compact)|schedules(?:/\d+/(?:start|stop|restart))?)",
    "PUT": r"/api/schedules/\d+",
    "DELETE": r"/api/schedules/\d+",
}


@dataclass
class Reply:
    payloads: list[object]
    status: int
    seen: dict[str, Any] = field(default_factory=dict)


class PublicGateway:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.replies: dict[str, Reply] = {}
        self.requests: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self.caller_profile = False
        self.calls = 0
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def parse_request(self) -> bool:
                accepted = super().parse_request()
                if accepted and self.command not in _ROUTES:
                    gateway.failures.append(f"unexpected method: {self.command} {self.path}")
                return accepted

            def log_message(self, format: str, *args: object) -> None:
                pass

            def exchange(self) -> None:
                try:
                    gateway.exchange(self)
                except Exception as exc:
                    gateway.failures.append(f"listener failed: {exc!r}")
                    self.send_error(500)

            def do_GET(self) -> None:
                self.exchange()

            def do_POST(self) -> None:
                self.exchange()

            def do_PUT(self) -> None:
                self.exchange()

            def do_DELETE(self) -> None:
                self.exchange()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def respond(self, method: str, payload: object, status: int = 200) -> dict[str, Any]:
        reply = Reply([payload], status)
        self.replies[method.upper()] = reply
        return reply.seen

    def sequence(self, method: str, payloads: list[object]) -> None:
        self.replies[method.upper()] = Reply(payloads, 200)

    def exchange(self, handler: BaseHTTPRequestHandler) -> None:
        route = urlsplit(handler.path)
        reply = self.replies.get(handler.command)
        if reply is None or not re.fullmatch(_ROUTES.get(handler.command, r"(?!)"), route.path):
            self.failures.append(f"unexpected request: {handler.command} {handler.path}")
            handler.send_error(500)
            return
        body = handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
        captured = {
            "method": handler.command,
            "url": self.url + route.path,
            "params": {
                key: values[0]
                for key, values in parse_qs(route.query, keep_blank_values=True).items()
            },
            "headers": dict(handler.headers),
            "body": body,
            "json": json.loads(body) if body else None,
        }
        self.requests.append(captured)
        assert handler.headers["Authorization"] == f"Bearer {WIRE_CREDENTIAL}"
        reply.seen.update(
            url=captured["url"],
            params=captured["params"],
            json=captured["json"],
            headers={"Authorization": handler.headers["Authorization"]},
        )
        payload = reply.payloads[0]
        if len(reply.payloads) > 1:
            reply.payloads.pop(0)
        data = json.dumps(payload).encode()
        handler.send_response(reply.status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)

    @contextmanager
    def serving(self) -> Generator[PublicGateway]:
        with self.server, ThreadPoolExecutor(max_workers=1, thread_name_prefix="cli-wire") as pool:
            serving = pool.submit(self.server.serve_forever, poll_interval=0.01)
            try:
                yield self
            finally:
                self.server.shutdown()
                serving.result()
        assert self.failures == []

    def run(
        self,
        *argv: str,
        input: str | None = None,
        mode: str = "parser",
        expect: Literal["", "http", "value", "provenance"] = "",
        status: int = 0,
        path: str = "",
        match: str = "",
    ) -> int:
        self.calls += 1
        receipt = self.home / f"result-{self.calls}.json"
        env = {
            "PATH": os.defpath,
            "MEIVA_HOME": str(self.home),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_PROCESS_PROFILE": "gateway",
            "MEIVA_GATEWAY_URL": self.url,
            "MEIVA_API_TOKEN": WIRE_CREDENTIAL,
            "MEIVA_TIMEZONE": "UTC",
        }
        if self.caller_profile:
            env["MEIVA_CALLER_IDENTITY"] = '{"kind":"external_agent","subject":"codex"}'
        result = run_bounded(
            [
                sys.executable,
                "-I",
                str(_CHILD),
                str(receipt),
                mode,
                expect,
                str(status),
                path,
                match,
                *argv,
            ],
            env=env,
            input=input,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert receipt.is_file(), result.stdout + result.stderr
        recorded = json.loads(receipt.read_text())
        assert result.returncode == recorded["result"], result.stdout + result.stderr
        assert recorded["expected_error"] == expect
        assert recorded["caller_profile"] == env.get("MEIVA_CALLER_IDENTITY")
        assert self.failures == []
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode


@pytest.fixture
def wire(tmp_path: Path) -> Generator[PublicGateway]:
    home = tmp_path / "cli-home"
    home.mkdir()
    with PublicGateway(home).serving() as gateway:
        yield gateway
