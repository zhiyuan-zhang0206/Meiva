# meiva:note
# Ask the IM bridge to accept one native alert group for durable delivery.
#
# The bridge's address and the caller's credentials are values the gateway root read once
# (`GatewayConfig.alerts.im_bridge_url`, `app.state.gateway_access`). A refusal, an unreachable
# bridge or a missing machine token leaves the group unaccepted (False); this never falls back to
# the legacy `/send` route and never stamps the group SENT, because delivery is the bridge's.

from __future__ import annotations

import logging

import httpx

from base.cluster.machine import GatewayAccess, GatewayApiTokenMissing, gateway_auth_headers
from base.telemetry.alerts.native import AlertGroupOrigin

__all__ = ["notify_alert_group"]

_log = logging.getLogger(__name__)
_ACCEPTANCE_TIMEOUT_S = 10.0


def notify_alert_group(group_id: int, *, bridge_url: str, access: GatewayAccess) -> bool:
    try:
        response = httpx.post(
            f"{bridge_url}/send/alert-outbound",
            json={"group_id": group_id, "source_origin": AlertGroupOrigin.NATIVE.value},
            headers=gateway_auth_headers(access),
            timeout=_ACCEPTANCE_TIMEOUT_S,
        )
        if response.status_code == 200:
            return True
        _log.warning(
            "native alert acceptance held group=%s status=%s", group_id, response.status_code
        )
    except (httpx.HTTPError, GatewayApiTokenMissing) as exc:
        _log.warning("native alert acceptance held group=%s class=%s", group_id, type(exc).__name__)
    return False
