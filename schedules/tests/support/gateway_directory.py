# meiva:note
# A gateway on a loopback port that speaks the agent-directory routes the schedules use.
#
# It answers `GET /api/agents` page by page from the pages a test scripts, reads of one agent's
# status and last message from the `agents` table a test fills, and accepts the resurrect,
# message, terminate and keyed-spawn routes with the replies the real gateway gives; a path
# listed in `refuse` answers 404 instead. Every request is recorded with its query and JSON
# body, so a test asserts what the SDK sent over the wire. A directory request no page was
# scripted for, and a read of an agent the table does not hold, is refused with 404, so a loop
# that asks for something nobody expected fails instead of reading an empty answer. Each
# keyed spawn answers the next id after `spawned_agent_id`.

from __future__ import annotations

import json
import re
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx

__all__ = [
    "AgentReply",
    "AgentRowSpec",
    "DirectoryPage",
    "GatewayRequest",
    "ServedGateway",
    "serve_gateway",
]

_STAMP = "2026-10-01T00:00:00+00:00"
_AGENT_PATH = re.compile(r"^/api/agents/(\d+)$")
_LAST_MESSAGE_PATH = re.compile(r"^/api/agents/(\d+)/last-message$")


@dataclass(frozen=True)
class AgentRowSpec:
    agent_id: int
    label: str | None
    status: str = "idling"
    spawner: str = "user"


# meiva:note
# One scripted directory page: the request it answers (`query` and the cursor the caller sent
# as `before_id`) and the rows and `next_cursor` it returns.
@dataclass(frozen=True)
class DirectoryPage:
    query: str
    before_id: int | None
    agents: tuple[AgentRowSpec, ...]
    next_cursor: int | None


# meiva:note
# What the gateway answers when one agent is read directly: its status and the text of its
# latest turn (`None` before it has produced any).
@dataclass(frozen=True)
class AgentReply:
    status: str
    last_message: str | None = None


@dataclass(frozen=True)
class GatewayRequest:
    method: str
    path: str
    query: dict[str, str]
    body: dict[str, object]
    headers: dict[str, str] = field(default_factory=dict[str, str], compare=False)


@dataclass
class ServedGateway:
    url: str
    requests: list[GatewayRequest] = field(default_factory=list[GatewayRequest])
    agents: dict[int, AgentReply] = field(default_factory=dict[int, AgentReply])
    spawned_agent_id: int = 400

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.url)

    def directory_cursors(self) -> list[str | None]:
        return [
            request.query.get("before_id")
            for request in self.requests
            if request.path == "/api/agents"
        ]


def _row(spec: AgentRowSpec) -> dict[str, object]:
    return {
        "agent_id": spec.agent_id,
        "label": spec.label,
        "status": spec.status,
        "spawner": spec.spawner,
        "fork_source_agent_id": None,
        "machine": "test-machine",
        "spawned_at": _STAMP,
        "last_active_at": _STAMP,
        "last_inbound_at": _STAMP,
        "heartbeat_paused_until": None,
    }


@contextmanager
def serve_gateway(
    pages: tuple[DirectoryPage, ...] = (), *, refuse: frozenset[str] = frozenset()
) -> Generator[ServedGateway]:
    served = ServedGateway(url="")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def seen_headers(self) -> dict[str, str]:
            return {key.lower(): value for key, value in self.headers.items()}

        def reply(self, status: int, payload: object) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            served.requests.append(
                GatewayRequest("GET", parts.path, query, {}, self.seen_headers())
            )
            if parts.path in refuse:
                self.reply(404, {"detail": "refused route"})
                return
            single = _AGENT_PATH.match(parts.path)
            last = _LAST_MESSAGE_PATH.match(parts.path)
            if single is not None or last is not None:
                match = single or last
                assert match is not None
                known = served.agents.get(int(match.group(1)))
                if known is None:
                    self.reply(404, {"detail": "unscripted agent"})
                elif single is not None:
                    self.reply(200, {"status": known.status})
                else:
                    self.reply(200, {"text": known.last_message})
                return
            if parts.path != "/api/agents":
                self.reply(404, {"detail": "unscripted route"})
                return
            wanted = (
                query.get("query", ""),
                int(query["before_id"]) if "before_id" in query else None,
            )
            for page in pages:
                if (page.query, page.before_id) == wanted:
                    self.reply(
                        200,
                        {
                            "agents": [_row(spec) for spec in page.agents],
                            "next_cursor": page.next_cursor,
                        },
                    )
                    return
            self.reply(404, {"detail": "unscripted directory page"})

        def do_POST(self) -> None:
            parts = urlsplit(self.path)
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body: dict[str, object] = json.loads(raw) if raw else {}
            served.requests.append(
                GatewayRequest("POST", parts.path, {}, body, self.seen_headers())
            )
            if parts.path in refuse:
                self.reply(404, {"detail": "refused route"})
            elif parts.path == "/api/keyed/v1/agents":
                spawns = [r for r in served.requests if r.path == "/api/keyed/v1/agents"]
                self.reply(200, {"id": served.spawned_agent_id + len(spawns) - 1})
            elif parts.path.endswith("/terminate"):
                self.reply(200, {"status": "enqueued", "open_tasks": None})
            elif parts.path.endswith("/resurrect"):
                self.reply(200, {"status": "spawned"})
            elif parts.path.endswith("/messages"):
                self.reply(200, {"inbound_id": 1})
            else:
                self.reply(404, {"detail": "unscripted route"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    served.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield served
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
