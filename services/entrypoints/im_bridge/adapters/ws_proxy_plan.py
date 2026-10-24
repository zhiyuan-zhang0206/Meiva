# meiva:note
# The keyword arguments of the lark SDK's websocket handshake, decided from two facts.
#
# The SDK pins `proxy=None` whenever the installed `websockets` has a `proxy` parameter
# (websockets 15 and later), which turns off its discovery of a proxy from the environment, and
# `lark.ws.Client` takes no proxy argument. With a proxy configured, omitting the argument
# leaves websockets' own default (discover from `HTTPS_PROXY` / `ALL_PROXY`, honoring `NO_PROXY`
# per target) in charge. Without one, the SDK's direct behavior stands. An `ALL_PROXY`-only
# environment stays direct: websockets maps no `all` scheme onto a websocket target.

from __future__ import annotations

__all__ = ["ws_connect_kwargs"]


# meiva:note
# `supports_proxy` is whether the installed `websockets.connect` has a `proxy` parameter; on
# older installs it rejects unknown keyword arguments, so nothing is emitted. `proxy_configured`
# is whether the launching environment names a proxy.
def ws_connect_kwargs(*, proxy_configured: bool, supports_proxy: bool) -> dict[str, None]:
    if not supports_proxy:
        return {}
    return {} if proxy_configured else {"proxy": None}
