# meiva:note
# An SDK client's wire into an in-process gateway app, with the faults a network has.
#
# `meiva.gateway_client.use_client` takes a real `httpx.Client`; its transport here hands each
# request to the app's `TestClient` and brings the answer back as an httpx response, so the SDK's
# own retry and error boundary run unchanged. The transport records every request that crosses it
# and can drop the responses: the request still reaches the app and commits, but the caller sees
# a read timeout, which is how a lost response looks to a client.

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config.tests.configuration import snapshot_process_config
from meiva.boot import derived_clients
from meiva.gateway_client import use_client
from meiva.sdk_surface import process_context

__all__ = ["AppTransport", "wired_sdk", "zero_retry_delay"]


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


# meiva:note
# The SDK's own bounded retry backs off 1/2/4 s of real time; a test that provokes it (a lost
# response) rebinds the process context's clients over the same configuration with a zero base
# delay, so the retry loop still runs every attempt and its waits cost nothing. The per-agent
# jitter offset stays: it is a property of the identity, not of the delay the test configures.
@pytest.fixture
def zero_retry_delay() -> Iterator[None]:
    bound = process_context.current_context()
    config = snapshot_process_config()
    config.set_field("gateway_client_retry_delay_seconds", 0.0)
    context = replace(bound, clients=derived_clients(config, ConfiguredMachineIdentity(config)))
    process_context.bind_context(context)
    try:
        yield
    finally:
        process_context.bind_context(bound)
        context.clients.close()
