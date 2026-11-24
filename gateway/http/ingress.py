# meiva:note
# Which public path a staged upload arrived on, read from its request headers: values in, a
# classification out.
#
# The public entry is Cloudflare, then cloudflared, then the gateway, so the socket peer is always
# loopback and says nothing about the client. Cloudflare's own headers do: `CF-Ray` ends in the
# three-letter code of the edge that took the request, and `CF-Connecting-IP` is present exactly
# when the request came through Cloudflare. A request that reached the gateway another way (the
# tailnet address, the LAN) carries neither. Only the colo suffix, a presence flag and the
# server-side `Host` name are kept: never the ray id and never the client address, which would
# identify one request or one person.

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

__all__ = ["UploadIngress", "classify_ingress"]

_COLO = re.compile(r"[A-Za-z]{2,5}")
_MAX_HOST_CHARS = 255


# meiva:note
# `cf_colo` is the uppercased edge code of a well-formed `CF-Ray`, `None` when the header is absent
# or does not end in one. `via_cloudflare` is whether `CF-Ray` was present at all, so a malformed
# ray still counts as Cloudflare. `cf_connecting_ip` is only whether that header was sent.
# `content_length` is the declared body size, `None` when absent or not a non-negative integer.
@dataclass(frozen=True)
class UploadIngress:
    via_cloudflare: bool
    cf_colo: str | None
    cf_connecting_ip: bool
    host: str | None
    content_length: int | None


def _colo(ray: str) -> str | None:
    suffix = ray.rpartition("-")[2]
    return suffix.upper() if _COLO.fullmatch(suffix) else None


def _hostname(host: str) -> str | None:
    value = host.strip().lower()
    name = value[1:].partition("]")[0] if value.startswith("[") else value.partition(":")[0]
    return name[:_MAX_HOST_CHARS] or None


def _declared_length(value: str | None) -> int | None:
    if value is None:
        return None
    digits = value.strip()
    return int(digits) if digits.isascii() and digits.isdigit() else None


# meiva:note
# `headers` is a case-insensitive mapping (Starlette's `Headers`) or a dict with lowercase keys.
def classify_ingress(headers: Mapping[str, str]) -> UploadIngress:
    ray = headers.get("cf-ray")
    host = headers.get("host")
    return UploadIngress(
        via_cloudflare=ray is not None,
        cf_colo=None if ray is None else _colo(ray),
        cf_connecting_ip="cf-connecting-ip" in headers,
        host=None if host is None else _hostname(host),
        content_length=_declared_length(headers.get("content-length")),
    )
