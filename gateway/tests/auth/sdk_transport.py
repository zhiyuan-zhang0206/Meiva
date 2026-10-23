# meiva:note
# An SDK client's wire into an in-process gateway app, with the faults a network has.
#
# `meiva.gateway_client.use_client` takes a real `httpx.Client`; its transport here hands each
# request to the app's `TestClient` and brings the answer back as an httpx response, so the SDK's
# own retry and error boundary run unchanged. The transport records every request that crosses it
# and can drop the responses: the request still reaches the app and commits, but the caller sees
# a read timeout, which is how a lost response looks to a client.

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

import httpx
from fastapi.testclient import TestClient

from meiva.gateway_client import use_client

__all__ = ["AppTransport", "wired_sdk"]


class AppTransport(httpx.BaseTransport):
    def __init__(self, target: TestClient) -> None:
        self.target = target
        self.requests: list[httpx.Request] = []
        self.losing_responses = False

    # meiva:note
    # The requests that crossed, as (method, path, body, idempotency key), duplicates merged.
    #
    # One logical intent retried any number of times is one entry.
    def distinct(self) -> set[tuple[str, str, bytes, str | None]]:
        return {
            (
                request.method,
                request.url.raw_path.decode(),
                request.content,
                request.headers.get("Idempotency-Key"),
            )
            for request in self.requests
        }

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.raw_path.decode()
        answer = self.target.request(
            request.method, path, content=request.content, headers=dict(request.headers)
        )
        if self.losing_responses:
            raise httpx.ReadTimeout(
                "the response was lost after the app committed", request=request
            )
        return httpx.Response(
            status_code=answer.status_code,
            headers=dict(answer.headers),
            content=answer.content,
            request=request,
        )


# meiva:note
# Route every SDK gateway call in the block into `target`, recording what crosses.
@contextmanager
def wired_sdk(target: TestClient) -> Generator[AppTransport]:
    transport = AppTransport(target)
    with (
        httpx.Client(
            transport=transport, base_url="http://testserver", timeout=httpx.Timeout(10.0)
        ) as sdk_client,
        use_client(sdk_client),
    ):
        yield transport
