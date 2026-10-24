# meiva:note
# A scripted gateway on a loopback port, for the verbs that dial `/api/cluster/*`.
#
# `serving` is the observability tests' scripted HTTP server (`serve`) speaking JSON: it answers
# each (method, path) with one scripted status, JSON body and delay, and records every request it
# saw. The command under test dials it through its real transport and its real headers, so nothing
# about the client is replaced. A request for a path nobody scripted answers 404, which is what the
# real gateway says for an unknown machine.

from __future__ import annotations

import json
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from base.cluster.machine import GatewayAccess
from cli.commands.observability.tests import scripted_http

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

    def scripted(self) -> scripted_http.Reply:
        payload = json.dumps(self.body).encode() if self.raw is None else self.raw
        return scripted_http.Reply(
            status=self.status,
            body=payload,
            content_type="application/json",
            delay_s=self.delay_s,
        )


# meiva:note
# One request as the gateway saw it: the full target (query included), the headers and the
# decoded JSON body, if any.
@dataclass(frozen=True)
class Seen:
    method: str
    target: str
    headers: Mapping[str, str]
    body: object


# meiva:note
# The running gateway: its base URL and the requests it has answered so far.
@dataclass(frozen=True)
class ScriptedGateway:
    url: str
    served: scripted_http.Served

    @property
    def requests(self) -> list[Seen]:
        return [
            Seen(
                request.method,
                request.target,
                request.headers,
                json.loads(request.body) if request.body else None,
            )
            for request in self.served.received
        ]


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
    scripted: dict[scripted_http.Route, scripted_http.Reply | Sequence[scripted_http.Reply]] = {
        key: reply.scripted() if isinstance(reply, Reply) else [item.scripted() for item in reply]
        for key, reply in routes.items()
    }
    with scripted_http.serve(scripted, otherwise=Reply(404, {"detail": "no"}).scripted()) as served:
        yield ScriptedGateway(url=served.url, served=served)
