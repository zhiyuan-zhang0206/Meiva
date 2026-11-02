# meiva:note
# The httpx failures that leave a gateway reachable on a later attempt, owned once for every caller
# that retries one. This module imports httpx at module level and so stays off the `import meiva`
# path: the SDK's gateway transport reads it inside the retry loop, where httpx is already loaded.
from __future__ import annotations

import httpx

__all__ = ["RETRYABLE_NETWORK_ERRORS"]

# meiva:note
# A timeout, a connection-level network error, a proxy failure and a peer that closed mid-reply all
# leave the gateway reachable on a later attempt. Every other httpx failure is deterministic — an
# unsupported protocol, a locally malformed request — and repeating it only delays the report.
RETRYABLE_NETWORK_ERRORS: tuple[type[Exception], ...] = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)
