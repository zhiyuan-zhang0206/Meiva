# meiva:note
# The existing timeline API, exposed unchanged as timeline/context in the CLI.

from __future__ import annotations

import json

from base.cluster.machine import GatewayAccess, gateway_api_base, gateway_auth_headers
from base.host.net import http_dial


# meiva:note
# Print the timeline's standing context and history window as JSON.
#
# Without ``limit`` the request omits it and the gateway applies its configured default
# window (display.timeline_default_limit).
def cmd_agents_timeline(
    access: GatewayAccess, agent_id: int, limit: int | None = None, before: str | None = None
) -> int:
    params: dict[str, int | str] = {}
    if limit is not None:
        params["limit"] = limit
    if before is not None:
        params["before"] = before
    response = http_dial.get(
        f"{gateway_api_base(access.gateway_url)}/api/agents/{agent_id}/timeline",
        params=params,
        headers=gateway_auth_headers(access),
        timeout=30.0,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), ensure_ascii=False))
    return 0
