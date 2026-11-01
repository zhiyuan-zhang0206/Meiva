"""Child process: hold the handoff snapshot writes of one generated scenario until released.

Usage: `child_budget_writer.py ROOT SCENARIO_CODE`; SCENARIO_CODE is the generated fixture program.
"""

import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import meiva
from base.agents.context import MeivaContext
from base.agents.context.identity import AgentIdentity
from base.cluster.machine import MachineFacts
from base.config import ConfigBoot
from meiva.boot import derived_clients

root = Path(sys.argv[1])
publications = [0]


def hold_publication(previous: str) -> None:
    publications[0] += 1
    publication = publications[0]
    (root / f"previous-{publication}").write_text(previous)
    (root / f"ready-{publication}").touch()
    deadline = time.monotonic() + 10
    while not (root / f"release-{publication}").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("Reader did not release publication")
        time.sleep(0.01)


original_open = Path.open


def held_open(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
    is_snapshot = path.name == "1.json" and mode == "w"
    previous = (root / "1.json").read_text() if is_snapshot else ""
    stream = original_open(path, mode, *args, **kwargs)
    if is_snapshot:
        hold_publication(previous)
    return stream


original_fdopen = os.fdopen


def held_fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
    previous = (root / "1.json").read_text() if mode == "w" else ""
    stream = original_fdopen(fd, mode, *args, **kwargs)
    if mode == "w":
        hold_publication(previous)
    return stream


class _GatewayHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        body = b'{"id": 2}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


server = HTTPServer(("127.0.0.1", 0), _GatewayHandler)
threading.Thread(target=server.serve_forever, daemon=True).start()
config = ConfigBoot(
    environment={
        "MEIVA_HOME": str(root),
        "MEIVA_CONFIG_FETCH": "skip",
        "MEIVA_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}",
        "MEIVA_API_TOKEN": "test-machine-token",
    }
)
machine = MachineFacts.of(
    name="test", role=frozenset({"agent-runner"}), host="localhost", description=""
)
clients = derived_clients(config, machine)
Path.open = held_open  # pyright: ignore[reportAttributeAccessIssue]
os.fdopen = held_fdopen  # pyright: ignore[reportAttributeAccessIssue]
meiva.ensure_plugins_loaded(
    config=config,
    machine=machine,
    context=MeivaContext(
        identity=AgentIdentity(agent_id=1, owns_loop=False),
        machine=machine,
        clients=clients,
        meiva_home=root,
        environ=config.process_environment(),
    ),
)
exec(sys.argv[2])
