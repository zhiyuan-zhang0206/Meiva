"""Shared fixtures for the integration tests (registered by `tests/fixtures/path_scopes.py`)."""

from gateway.tests.auth.integration_tests import (
    gateway_client as gateway_client,
)
from gateway.tests.auth.integration_tests import (
    local_spawn_in_process as local_spawn_in_process,
)
from gateway.tests.auth.integration_tests import (
    mock_api_keys as mock_api_keys,
)
from gateway.tests.auth.integration_tests import (
    model_catalog as model_catalog,
)

__all__ = [
    "gateway_client",
    "local_spawn_in_process",
    "mock_api_keys",
    "model_catalog",
]
