import json
from types import SimpleNamespace
from typing import Any

import pytest

from services.agent_runner.agent_host.health_routes import prepare_harness_switch_route


def _host(dropped: list[int]) -> Any:
    return SimpleNamespace(drop_agent=dropped.append)


async def test_preparing_a_switch_drops_the_agents_cached_runtime_and_writes_no_history() -> None:
    dropped: list[int] = []

    status_code, body, _content_type = await prepare_harness_switch_route(_host(dropped))(
        json.dumps({"agent_id": 9211}).encode("utf-8")
    )

    assert status_code == 200
    assert b'"ok":true' in body
    assert dropped == [9211]


@pytest.mark.parametrize("payload", [b"{}", b'{"agent_id": 0}', b'{"agent_id": "7"}', b"[]"])
async def test_a_payload_without_a_positive_agent_id_is_refused(payload: bytes) -> None:
    dropped: list[int] = []

    status_code, _body, _content_type = await prepare_harness_switch_route(_host(dropped))(payload)

    assert status_code == 400
    assert dropped == []
