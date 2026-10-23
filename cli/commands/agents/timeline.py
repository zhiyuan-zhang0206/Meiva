"""The existing timeline API, exposed unchanged as timeline/context in the CLI."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cli.commands.agents.control import AgentsTransport


def cmd_agents_timeline(
    agent_id: int,
    limit: int | None = None,
    before: str | None = None,
    *,
    transport: AgentsTransport | None = None,
) -> int:
    """Print the timeline's standing context and history window as JSON.

    Without ``limit`` the request omits it and the gateway applies its
    configured default window (display.timeline_default_limit)."""
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.cluster.machine.ambient import gateway_access_from_settings
    from base.host.net.http_dial import get

    params: dict[str, int | str] = {}
    if limit is not None:
        params["limit"] = limit
    if before is not None:
        params["before"] = before
    if transport is not None:
        active_get = transport.get or get
        base_url = transport.gateway_base()
        headers = transport.headers()
    else:
        access = gateway_access_from_settings()  # ambient: C
        active_get = get
        base_url = gateway_api_base(access.gateway_url)
        headers = gateway_auth_headers(access)
    response = active_get(
        f"{base_url}/api/agents/{agent_id}/timeline",
        params=params,
        headers=headers,
        timeout=30.0,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False))
    return 0
