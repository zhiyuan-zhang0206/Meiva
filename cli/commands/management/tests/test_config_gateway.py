from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from base.api_contracts.config import ConfigView
from cli.commands.management.tests.config_gateway import ConfigGateway


def test_serving_closes_its_socket_after_requests(tmp_path: Path) -> None:
    view = ConfigView(fields=[], raw_overrides={}, machine_capabilities=[])
    gateway = ConfigGateway(tmp_path, view)

    with gateway.serving():
        response = httpx.get(f"{gateway.url}/api/config", timeout=3, trust_env=False)
        assert response.status_code == 200
        assert response.json() == view.model_dump()
    assert gateway.server.fileno() == -1


def test_serving_propagates_its_server_loop_failure(tmp_path: Path) -> None:
    gateway = ConfigGateway(
        tmp_path, ConfigView(fields=[], raw_overrides={}, machine_capabilities=[])
    )
    gateway.server.server_close()

    with pytest.raises(ValueError, match="Invalid file descriptor"), gateway.serving():
        pass
    assert gateway.server.fileno() == -1
