"""`scripts/entrypoints/agent.py` presents the cluster secret on its bootstrap spawn.

The keyed spawn route is authenticated; the bootstrap-spawn script must carry
`Authorization: Bearer <cluster secret>` or a multi-host gateway 401s it, plus the
keyed-admission headers.
"""

from __future__ import annotations

from base.cluster.machine import GatewayAccess
from scripts.entrypoints import agent as mod


def test_start_agent_posts_with_bearer() -> None:
    expected = "sekret"
    access = GatewayAccess(
        gateway_url="http://gw:8000",
        cluster_secret=expected,
        remote_plane=False,
        machine_token=None,
        launcher_profile=None,
        gateway_home=True,
    )
    request = mod.build_spawn_request(access, idempotency_key="key-123")

    assert request.url == "http://gw:8000/api/keyed/v1/agents"
    assert request.json == {"spawner": "user"}
    assert request.headers["Authorization"] == "Bearer sekret"
    assert request.headers["Idempotency-Scope"] == "principal-v1"
    assert request.headers["Idempotency-Key"] == "key-123"
    assert mod.parse_spawn_agent_id({"id": 7}) == 7
