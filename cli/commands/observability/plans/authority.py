# meiva:note
# How a host, a port and a URL's authority are written and classified in rendered config.

from __future__ import annotations

import ipaddress

from cli.commands.observability.plans.url_view import UrlView

__all__ = [
    "has_loopback_hostname",
    "host_port",
    "is_specific_non_loopback_listen",
    "is_unspecified_address",
    "url_host_port",
]


# meiva:note
# `host:port`, with brackets around an IPv6 literal so the colon-separated parts stay unambiguous.
def host_port(host: str, port: int | str) -> str:
    bare = host.strip().removeprefix("[").removesuffix("]")
    rendered = f"[{bare}]" if ":" in bare else bare
    return f"{rendered}:{port}"


# meiva:note
# True for the wildcard IP literals (0.0.0.0 and ::), never for a hostname.
def is_unspecified_address(host: str) -> bool:
    candidate = host.strip().removeprefix("[").removesuffix("]")
    try:
        return ipaddress.ip_address(candidate).is_unspecified
    except ValueError:
        return False


# meiva:note
# Whether a URL's host is `localhost` or a loopback address.
def has_loopback_hostname(url: UrlView) -> bool:
    hostname = url.hostname
    if hostname is None:
        return False
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


# meiva:note
# `host:port` of a base URL with the scheme and the trailing slash removed: the form a
# Prometheus scrape target takes.
def url_host_port(url: str) -> str:
    return url.rstrip("/").removeprefix("http://").removeprefix("https://")


# meiva:note
# Whether a listen host binds one specific address that loopback dials miss.
#
# A wildcard bind and a loopback bind still answer on loopback, so neither breaks the default
# loopback read URLs. Only a specific non-loopback IP does. A hostname is not judged: resolving
# it is out of scope here, and the external-migration form uses literal private IPs.
def is_specific_non_loopback_listen(host: str) -> bool:
    bare = host.strip().lower().removeprefix("[").removesuffix("]")
    if bare in ("0.0.0.0", "::", "localhost", "::1") or bare.startswith("127."):  # noqa: S104
        return False
    try:
        return not ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False
