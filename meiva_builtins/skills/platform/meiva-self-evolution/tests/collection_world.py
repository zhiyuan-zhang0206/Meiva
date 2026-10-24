# meiva:note
# The world a collection run reads, for the self-evolution tests: a real `/api/events` listener on port 0 and the rows the database side needs. `serve_events` answers like the gateway does (filters by category, agent and the window inclusive on both ends, pages newest-first, reports only `has_more`) and can script the first replies of a test (an HTTP status with headers and a body, or a dropped connection) before it answers for real; it records every request it received. `grant` builds the `GatewayAccess` value a test presents. `insert_agent` and `insert_inbound` seed the lifecycle and inbound rows that `collect.py` reads from the throwaway database.
from __future__ import annotations

import json
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import psycopg

from base.cluster.machine import GatewayAccess

BASE = datetime.fromisoformat("2026-08-13T00:00:00+00:00")


def ts_after(seconds: float) -> datetime:
    return BASE + timedelta(seconds=seconds)


def event_row(
    row_id: int,
    ts: datetime,
    *,
    category: str = "telemetry",
    agent_id: int = 7,
    event_name: str = "turn_end",
    attributes: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": row_id,
        "ts": ts.isoformat(),
        "agent_id": agent_id,
        "category": category,
        "event_name": event_name,
        "attributes": attributes if attributes is not None else {"i": row_id},
    }


# meiva:note
# A scripted reply that is not the real answer: an HTTP status with its headers and body, or (`status=None`) a connection closed without any reply.
@dataclass(frozen=True)
class Scripted:
    status: int | None
    headers: dict[str, str] = field(default_factory=dict[str, str])
    body: str = ""


@dataclass(frozen=True)
class Received:
    path: str
    params: dict[str, str]
    headers: dict[str, str]


# meiva:note
# The running listener: its URL, the requests it received in order, and the scripted replies not yet used.
@dataclass
class EventsServer:
    url: str
    rows: list[dict[str, object]]
    script: list[Scripted]
    requests: list[Received] = field(default_factory=list[Received])

    def answer(self, params: dict[str, str]) -> dict[str, object]:
        agent = params.get("agent_id")
        window_from, window_to = params["from"], params["to"]
        matching = sorted(
            (
                row
                for row in self.rows
                if row["category"] == params["category"]
                and (agent is None or str(row["agent_id"]) == agent)
                and window_from <= str(row["ts"]) <= window_to
            ),
            key=lambda row: str(row["ts"]),
            reverse=True,
        )
        limit, offset = int(params["limit"]), int(params["offset"])
        page = matching[offset : offset + limit]
        return {"items": page, "meta": {"has_more": offset + len(page) < len(matching)}}


@contextmanager
def serve_events(
    rows: Sequence[dict[str, object]] = (), script: Sequence[Scripted] = ()
) -> Generator[EventsServer]:
    served = EventsServer(url="", rows=list(rows), script=list(script))

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            split = urlsplit(self.path)
            params = {key: values[0] for key, values in parse_qs(split.query).items()}
            served.requests.append(
                Received(split.path, params, {k.lower(): v for k, v in self.headers.items()})
            )
            if served.script:
                scripted = served.script.pop(0)
                if scripted.status is None:
                    self.close_connection = True
                    return
                self._reply(scripted.status, scripted.body, scripted.headers)
                return
            self._reply(
                200, json.dumps(served.answer(params)), {"Content-Type": "application/json"}
            )

        def _reply(self, status: int, body: str, headers: dict[str, str]) -> None:
            data = body.encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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


def grant(
    url: str,
    *,
    human_bearer: str = "test-secret",
    delivered_bearer: str | None = None,
    profile: str | None = None,
) -> GatewayAccess:
    return GatewayAccess(
        gateway_url=url,
        cluster_secret=human_bearer,
        remote_plane=False,
        machine_token=delivered_bearer,
        launcher_profile=profile,
        gateway_home=False,
    )


def insert_agent(
    cur: psycopg.Cursor,
    agent_id: int,
    *,
    spawner: str = "user",
    label: str | None = None,
    status: str = "idling",
    last_message: str | None = None,
) -> None:
    cur.execute(
        "INSERT INTO agents (id, label) VALUES (%s, %s)",
        (agent_id, label if label is not None else f"test-agent-{agent_id}"),
    )
    cur.execute(
        "INSERT INTO agents_meta (id, spawner, status, last_message_text) VALUES (%s, %s, %s, %s)",
        (agent_id, spawner, status, last_message),
    )


def insert_inbound(cur: psycopg.Cursor, agent_id: int, source: str, content: str, age: str) -> None:
    cur.execute(
        "INSERT INTO inbound_messages (agent_id, source, content, kind, created_at) "
        "VALUES (%s, %s, %s, 'chat', now() - %s::interval)",
        (agent_id, source, content, age),
    )
