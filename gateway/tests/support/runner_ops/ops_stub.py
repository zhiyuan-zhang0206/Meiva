# meiva:note
# A runner's `POST /ops` endpoint as a real listener on port 0, registered in `machines`.
#
# The gateway reaches a runner only through `ops.cluster.rpc.dispatch_to_machine`: it reads the
# target's `gateway_url` from the registry and POSTs an `OpEnvelope` there. A test that needs a
# runner's answer serves it from this listener and registers the machine, so the real registry
# read, the real HTTP client and the real result handling all run. The test then asserts on the
# HTTP response and on `calls`, the envelopes that arrived on the wire.
#
# An answer is a value (`completed`, `failed`, `refused`) or a function of the arriving call when
# the answer depends on it (the agent id a launch carries). A kind nobody scripted is answered
# with 404 and is recorded, so the gateway fails the test visibly without a retry delay.

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast

import psycopg

__all__ = [
    "OpsCall",
    "OpsReply",
    "Responder",
    "RunnerOps",
    "completed",
    "failed",
    "launched",
    "refused",
    "register_machine",
    "runner_ops",
]


# meiva:note
# One envelope that reached the listener.
@dataclass(frozen=True)
class OpsCall:
    kind: str
    payload: dict[str, Any]
    idempotency_key: str | None


# meiva:note
# What the listener sends back: an HTTP status and its JSON body (or plain text).
@dataclass(frozen=True)
class OpsReply:
    http_status: int
    body: dict[str, Any] | str


Responder = OpsReply | Callable[[OpsCall], OpsReply]


# meiva:note
# The op ran and produced `result`.
def completed(result: Mapping[str, Any]) -> OpsReply:
    return OpsReply(200, {"status": "completed", "result": dict(result)})


# meiva:note
# The op ran and failed; `result` is the `OpFailure` dict (`error`, `detail`, `reason`).
def failed(result: Mapping[str, Any]) -> OpsReply:
    return OpsReply(200, {"status": "failed", "result": dict(result)})


# meiva:note
# The listener answers with a bare HTTP status. A 5xx is retried by the dispatcher.
def refused(http_status: int, text: str = "") -> OpsReply:
    return OpsReply(http_status, text)


# meiva:note
# A runner that accepts a `spawn-launch`: it answers for the agent the call carries.
def launched(call: OpsCall) -> OpsReply:
    return completed({"id": call.payload["agent_id"]})


# meiva:note
# A running listener and the calls it has received.
@dataclass
class RunnerOps:
    name: str
    url: str
    calls: list[OpsCall] = field(default_factory=list)
    errors: list[BaseException] = field(default_factory=list)

    def calls_of(self, kind: str) -> list[OpsCall]:
        return [call for call in self.calls if call.kind == kind]

    def payloads(self, kind: str) -> list[dict[str, Any]]:
        return [call.payload for call in self.calls_of(kind)]


# meiva:note
# Write the `machines` row the gateway reads for `name` and commit it.
def register_machine(
    conn: psycopg.Connection,
    name: str,
    *,
    roles: tuple[str, ...] = ("agent-runner",),
    url: str | None = None,
    paused: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO machines (name, role, gateway_url, paused_at) "
        "VALUES (%s, %s, %s, CASE WHEN %s THEN now() END) "
        "ON CONFLICT (name) DO UPDATE SET role = EXCLUDED.role, "
        "gateway_url = EXCLUDED.gateway_url, paused_at = EXCLUDED.paused_at",
        (name, list(roles), url, paused),
    )
    conn.commit()


def _handler(
    runner: RunnerOps, replies: Mapping[str, Responder], otherwise: Responder | None
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            envelope = cast(dict[str, Any], json.loads(self.rfile.read(length) or b"{}"))
            call = OpsCall(
                kind=envelope["kind"],
                payload=envelope.get("payload", {}),
                idempotency_key=envelope.get("idempotency_key"),
            )
            runner.calls.append(call)
            reply = replies.get(call.kind, otherwise)
            try:
                answer = (
                    OpsReply(404, {"detail": f"no scripted reply for {call.kind!r}"})
                    if reply is None
                    else reply(call)
                    if callable(reply)
                    else reply
                )
            except BaseException as exc:
                runner.errors.append(exc)
                answer = OpsReply(400, {"detail": f"scripted reply raised {exc!r}"})
            data = (
                answer.body.encode()
                if isinstance(answer.body, str)
                else json.dumps(answer.body).encode()
            )
            self.send_response(answer.http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


# meiva:note
# Serve `replies` (op kind -> answer) as machine `name` and register it.
#
# A kind that `replies` does not name goes to `otherwise`; with none, it is answered 404.
#
# An exception a scripted function raised is re-raised on exit, so a bug in a test's answer
# fails that test rather than turning into a gateway error nobody reads.
@contextmanager
def runner_ops(
    conn: psycopg.Connection,
    name: str,
    replies: Mapping[str, Responder] | None = None,
    *,
    roles: tuple[str, ...] = ("agent-runner",),
    paused: bool = False,
    otherwise: Responder | None = None,
) -> Generator[RunnerOps]:
    runner = RunnerOps(name=name, url="")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(runner, replies or {}, otherwise))
    server.daemon_threads = True
    runner.url = f"http://127.0.0.1:{server.server_port}"
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    serving.start()
    try:
        register_machine(conn, name, roles=roles, url=runner.url, paused=paused)
        yield runner
    finally:
        server.shutdown()
        server.server_close()
        serving.join()
    if runner.errors:
        raise runner.errors[0]
