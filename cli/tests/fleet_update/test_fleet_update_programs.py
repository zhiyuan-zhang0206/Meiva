from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from base.paths import repo_root
from cli.tests.fleet_update.fleet_update_cluster import assert_isolated_home

_ROSTER = {
    "name": "runner-a",
    "online": True,
    "identity_mismatch": False,
    "head_sha": "target-sha",
    "running_sha": "target-sha",
    "serve_agent_runner": True,
}


@dataclass
class Gateway:
    url: str = ""
    secret: str = field(default_factory=lambda: uuid.uuid4().hex)
    calls: list[tuple[str, str, dict[str, str], object]] = field(
        default_factory=list[tuple[str, str, dict[str, str], object]]
    )

    def respond(self, method: str, path: str) -> tuple[int, object]:
        if (method, path) == ("GET", "/api/cluster/roster?fresh=true"):
            return 200, [{**_ROSTER, "not_in_public_summary": "discard"}]
        return 404, {"error": "unexpected request"}


@pytest.fixture
def gateway() -> Iterator[Gateway]:
    state = Gateway()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else None
            headers = {key.lower(): value for key, value in self.headers.items()}
            state.calls.append((self.command, self.path, headers, body))
            if headers.get("authorization") != f"Bearer {state.secret}":
                status, response = 401, {"error": "wrong credential"}
            else:
                status, response = state.respond(self.command, self.path)
            raw = json.dumps(response).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        assert not thread.is_alive()


def run_program(tmp_path: Path, gateway: Gateway, *args: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text(
        f"MEIVA_GATEWAY_URL={gateway.url}\nMEIVA_CLUSTER_SECRET={gateway.secret}\nMEIVA_TIMEZONE=UTC\n"
    )
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "MEIVA_HOME": str(home),
        "MEIVA_CONFIG_FETCH": "skip",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    assert_isolated_home(env["HOME"])
    return subprocess.run(
        [sys.executable, "-m", "cli.commands.management.fleet_gateway", *args],
        cwd=repo_root(),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_roster_program_uses_current_configuration_and_gateway_api(
    tmp_path: Path, gateway: Gateway
) -> None:
    proc = run_program(tmp_path, gateway, "roster")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == [_ROSTER]
    assert len(gateway.calls) == 1
    method, path, headers, body = gateway.calls[0]
    assert (method, path, body) == ("GET", "/api/cluster/roster?fresh=true", None)
    assert headers["authorization"] == f"Bearer {gateway.secret}"
    assert gateway.secret not in proc.stdout + proc.stderr


def test_gateway_program_refuses_unknown_verb_without_calling_gateway(
    tmp_path: Path, gateway: Gateway
) -> None:
    proc = run_program(tmp_path, gateway, "smoke", "runner-a", "10")
    assert proc.returncode == 2
    assert "usage: fleet_gateway.py roster" in proc.stderr
    assert gateway.calls == []
    assert gateway.secret not in proc.stdout + proc.stderr
