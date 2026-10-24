# meiva:note
# A gateway stand-in for the receiver's object pull: a real ASGI application on a loopback
# port that serves the bytes of one batch's objects and records what each pull carried.
#
# `serve_objects` answers `GET /api/keyed/v1/agents/<agent>/uploads/<batch>/objects/<n>` with
# the stored bytes, 404 for an object taken away with `ServedObjects.gone`. When it is given a
# connection pool it borrows a connection from it with a short timeout on every pull, so a
# receiver that still held its only pooled connection while it was on the network fails the
# pull.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field

from psycopg_pool import ConnectionPool
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from base.host.net.tests.local_http import serve_asgi

__all__ = ["Pull", "ServedObjects", "serve_objects"]


@dataclass(frozen=True)
class Pull:
    ordinal: int
    authorization: str | None


@dataclass
class ServedObjects:
    url: str
    pulls: list[Pull] = field(default_factory=list[Pull])
    gone: set[int] = field(default_factory=set[int])


@contextmanager
def serve_objects(
    *, agent_id: int, batch_id: str, objects: dict[int, bytes], pool: ConnectionPool | None = None
) -> Generator[ServedObjects]:
    served = ServedObjects(url="")

    def pull(request: Request) -> Response:
        ordinal = int(request.path_params["ordinal"])
        served.pulls.append(Pull(ordinal, request.headers.get("authorization")))
        if pool is not None:
            with pool.connection(timeout=0.5) as conn:
                conn.execute("SELECT 1")
        if ordinal in served.gone or ordinal not in objects:
            return Response(status_code=404)
        return Response(objects[ordinal], media_type="application/octet-stream")

    route = Route(
        f"/api/keyed/v1/agents/{agent_id}/uploads/{batch_id}/objects/{{ordinal:int}}", pull
    )
    with serve_asgi(Starlette(routes=[route])) as url:
        served.url = url
        yield served
