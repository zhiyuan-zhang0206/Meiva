# meiva:note
# A scripted gateway on a loopback port, for the verbs that dial `/api/cluster/*`.
#
# `serving` stands up a real `ThreadingHTTPServer` on port 0 that answers each (method, path)
# with one scripted status, JSON body and delay, and records every request it saw: the
# command under test dials it through its real transport and its real headers, so nothing
# about the client is replaced. A request for a path nobody scripted answers 404, which is
# what the real gateway says for an unknown machine.

from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from base.cluster.machine import GatewayAccess

__all__ = ["SECRET", "Reply", "ScriptedGateway", "Seen", "access_to", "serving"]

# meiva:note
# The secret is a literal of the throwaway gateway these tests serve; it never leaves the
# loopback.
SECRET = "test-cluster-secret"  # noqa: S105


# meiva:note
# One scripted answer. `delay_s` is how long the gateway sits on the request first; `raw`, when
# given, is sent as the body bytes instead of `body` encoded as JSON.
@dataclass(frozen=True)
class Reply:
    status: int = 200
    body: object = None
    delay_s: float = 0.0
    raw: bytes | None = None


# meiva:note
# One request as the gateway saw it: the full target (query included), the headers and the
# decoded JSON body, if any.
@dataclass(frozen=True)
class Seen:
    method: str
    target: str
    headers: Mapping[str, str]
    body: object


@dataclass
class ScriptedGateway:
    url: str
    requests: list[Seen] = field(default_factory=list[Seen])


# meiva:note
# What a command dials with when its gateway is `url`: the human cluster secret is the
# bearer, as on a gateway home.
def access_to(url: str) -> GatewayAccess:
    return GatewayAccess(
        gateway_url=url,
        cluster_secret=SECRET,
        remote_plane=False,
        machine_token=None,
        launcher_profile=None,
        gateway_home=True,
    )


# meiva:note
# Serve `routes` (keyed by method and path, query ignored) until the block ends. A route given
# a sequence of replies answers them in turn and repeats the last one.
@contextmanager
def serving(
    routes: Mapping[tuple[str, str], Reply | Sequence[Reply]],
) -> Generator[ScriptedGateway]:
    gateway = ScriptedGateway(url="")
    answered: dict[tuple[str, str], int] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _answer(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            gateway.requests.append(
                Seen(
                    method,
                    self.path,
                    {key.lower(): value for key, value in self.headers.items()},
                    json.loads(raw) if raw else None,
                )
            )
            key = (method, urlsplit(self.path).path)
            scripted = routes.get(key, Reply(404, {"detail": "no"}))
            if isinstance(scripted, Reply):
                reply = scripted
            else:
                reply = scripted[min(answered.get(key, 0), len(scripted) - 1)]
                answered[key] = answered.get(key, 0) + 1
            if reply.delay_s:
                time.sleep(reply.delay_s)
            payload = json.dumps(reply.body).encode() if reply.raw is None else reply.raw
            with suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        def do_GET(self) -> None:
            self._answer("GET")

        def do_POST(self) -> None:
            self._answer("POST")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    gateway.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield gateway
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
