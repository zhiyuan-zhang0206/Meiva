from __future__ import annotations

from typing import Any

import pytest

from base.cluster.machine import GatewayApiTokenMissing
from meiva_builtins.skills.tests.skill_script import load_skill_script

world = load_skill_script("platform", "meiva-self-evolution", "tests", "collection_world.py")
window_mod = load_skill_script("platform", "meiva-self-evolution", "scripts", "events_window.py")


@pytest.fixture
def collect_mod() -> Any:
    return load_skill_script("platform", "meiva-self-evolution", "scripts", "collect.py")


def _fetch(collect_mod: Any, gateway: Any) -> None:
    window = window_mod.Window(world.ts_after(0), world.ts_after(3600))
    collect_mod.fetch_events_window("audit", window, gateway=gateway)


def test_an_agent_process_presents_the_delivered_token(collect_mod: Any) -> None:
    with world.serve_events([]) as server:
        _fetch(
            collect_mod,
            world.grant(server.url, delivered_bearer="delivered-token", profile="agent"),
        )

    assert [request.headers["authorization"] for request in server.requests] == [
        "Bearer delivered-token"
    ]


def test_an_agent_process_without_a_token_fails_instead_of_reading_the_secret(
    collect_mod: Any,
) -> None:
    with (
        world.serve_events([]) as server,
        pytest.raises(GatewayApiTokenMissing, match="MEIVA_API_TOKEN"),
    ):
        _fetch(collect_mod, world.grant(server.url, profile="agent"))

    assert server.requests == []
