"""Env-proxy support for the lark-oapi WS handshake (issue #2089).

``lark_oapi`` pins ``proxy=None`` whenever the installed websockets exposes the
``proxy`` parameter (websockets>=15 — preserving its historical direct
connection), and ``lark.ws.Client`` takes no proxy parameter, so on a host
behind an HTTP proxy the long connection can never be established. The SDK's
module-level connect-kwargs builder is the only seam, and this module owns its
replacement so the adapter stays about bridging messages.

Wired in by ``FeishuAdapter._build_ws_client`` on the ws thread, before the
client is built: :func:`allow_env_proxy_for_ws`.
"""

from __future__ import annotations

import inspect
from functools import partial

from services.entrypoints.im_bridge.adapters.ws_proxy_plan import ws_connect_kwargs


# meiva:note
# Hand the SDK's kwargs builder over to `ws_connect_kwargs`, bound to what the process root
# knows: whether the launching environment names a proxy (the root reads it through the env
# registry's `network_proxy_configured`, so the WS path and the child env agree on one list of
# keys) and whether the installed `websockets.connect` has a `proxy` parameter.
#
# `Client._connect` resolves the builder by name at connect time, so replacing the module
# attribute is enough; it is called on the ws thread before the client is built. The SDK offers
# no proxy parameter and no other seam, so the private module attribute is the one place this
# patches third-party code. The replacement is a superset of the SDK's behavior: a host with no
# proxy configured keeps connecting directly.
def allow_env_proxy_for_ws(*, proxy_configured: bool) -> None:
    import lark_oapi.ws.client as ws_client  # pyright: ignore[reportUnknownVariableType]
    import websockets

    supports_proxy = "proxy" in inspect.signature(websockets.connect).parameters
    builder = partial(
        ws_connect_kwargs, proxy_configured=proxy_configured, supports_proxy=supports_proxy
    )
    setattr(ws_client, "_ws_connect_kwargs", builder)  # noqa: B010
