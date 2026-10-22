# meiva:note
# Shared fixtures for the integration tests (registered by `tests/fixtures/path_scopes.py`).

from collections.abc import Iterator

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient

from base.cluster.machine import MachineIdentity
from base.config.tests.api_keys import mock_api_keys as mock_api_keys
from base.packages.tests.model_catalog import model_catalog as model_catalog
from gateway.tests.auth.gateway_config import gateway_test_client
from gateway.tests.auth.gateway_tests import local_spawn_in_process as local_spawn_in_process
from meiva.gateway_client import use_client

__all__ = [
    "gateway_client",
    "local_spawn_in_process",
    "mock_api_keys",
    "model_catalog",
]


class _TestClientTransport(httpx.BaseTransport):
    # meiva:note
    # Forward httpx requests to FastAPI TestClient, in-process communication.

    def __init__(self, test_client: TestClient):
        self._tc = test_client

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method = request.method
        url = str(request.url)
        path = url.replace("http://testserver", "")
        headers = dict(request.headers)
        content = request.content
        if method == "POST":
            r = self._tc.post(path, content=content, headers=headers)
        elif method == "GET":
            r = self._tc.get(path, headers=headers)
        else:
            r = self._tc.request(method, path, content=content, headers=headers)
        return httpx.Response(
            status_code=r.status_code,
            headers=dict(r.headers),
            content=r.content,
            request=request,
        )


@pytest.fixture
def gateway_client(
    gateway_data_plane: None,
    db_conn: psycopg.Connection,
    machine_identity: MachineIdentity,
) -> Iterator[httpx.Client]:
    """Point the SDK's gateway client at the in-process gateway app (a TestClient transport)."""
    with gateway_test_client(machine=machine_identity) as test_client:
        transport = _TestClientTransport(test_client)
        with (
            httpx.Client(
                transport=transport, base_url="http://testserver", timeout=httpx.Timeout(10.0)
            ) as client,
            use_client(client),
        ):
            yield client
