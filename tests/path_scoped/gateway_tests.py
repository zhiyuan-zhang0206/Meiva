# meiva:note
# Gateway test fixtures (registered by `tests/fixtures/path_scopes.py`).

from __future__ import annotations

from gateway.tests.auth.gateway_tests import (
    local_config_in_process as local_config_in_process,
)
from gateway.tests.auth.gateway_tests import (
    local_lifecycle_in_process as local_lifecycle_in_process,
)
from gateway.tests.auth.gateway_tests import (
    local_spawn_in_process as local_spawn_in_process,
)
from gateway.tests.auth.gateway_tests import (
    mock_api_keys as mock_api_keys,
)
from gateway.tests.auth.gateway_tests import (
    model_catalog as model_catalog,
)

__all__ = [
    "local_config_in_process",
    "local_lifecycle_in_process",
    "local_spawn_in_process",
    "mock_api_keys",
    "model_catalog",
]
