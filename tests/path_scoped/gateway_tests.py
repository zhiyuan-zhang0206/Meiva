# meiva:note
# Gateway test fixtures (registered by `tests/fixtures/path_scopes.py`).

from __future__ import annotations

from gateway.tests.auth.gateway_tests import (
    gateway_local_runner as gateway_local_runner,
)
from gateway.tests.auth.gateway_tests import (
    gateway_tests_data_plane as gateway_tests_data_plane,
)
from gateway.tests.auth.gateway_tests import (
    mock_api_keys as mock_api_keys,
)
from gateway.tests.auth.gateway_tests import (
    model_catalog as model_catalog,
)

__all__ = [
    "gateway_local_runner",
    "gateway_tests_data_plane",
    "mock_api_keys",
    "model_catalog",
]
