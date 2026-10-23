from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self, cast
from urllib.parse import parse_qs, urlsplit

from base.api_contracts.config import ConfigAuditView, ConfigView, ConfigWriteResult
from base.config import runtime_config
from base.host.proc import run_bounded

_CHILD = Path(__file__).with_name("child_config_cmd.py")


def run_config(
    home: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return run_bounded(
        [sys.executable, "-I", str(_CHILD), *args],
        cwd=home,
        env={
            "PATH": os.environ["PATH"],
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            **(env or {}),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


class ConfigGateway:
    def __init__(self, home: Path, view: ConfigView) -> None:
        self.home = home
        self.view = view
        self.result = ConfigWriteResult(applied=True, results={}, restart_required=["agent"])
        self.audit = ConfigAuditView(records=[])
        self.body: dict[str, object] | None = None
        self.machine: str | None = None
        self.requests: list[tuple[str, str]] = []
        self.persist = False
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                gateway.requests.append(("GET", self.path))
                path = urlsplit(self.path).path
                if path == "/api/config":
                    self.respond(gateway.view.model_dump())
                elif path == "/api/config/audit":
                    self.respond(gateway.audit.model_dump())
                else:
                    self.send_error(404)

            def do_PUT(self) -> None:
                gateway.requests.append(("PUT", self.path))
                if urlsplit(self.path).path != "/api/config":
                    self.send_error(404)
                    return
                query = parse_qs(urlsplit(self.path).query)
                gateway.machine = query["machine"][0] if "machine" in query else None
                gateway.body = cast(
                    dict[str, object],
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
                if gateway.persist:
                    runtime_config.write_fields(gateway.body, set())
                self.respond(gateway.result.model_dump())

            def respond(self, payload: object) -> None:
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}
        )

    def __enter__(self) -> Self:
        (self.home / ".env").write_text(f"MEIVA_GATEWAY_URL={self.url}\n", encoding="utf-8")
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()

    def run(
        self, verb: str, *values: str, machine: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return run_config(self.home, f"remote-{verb}", machine or "", *values)
