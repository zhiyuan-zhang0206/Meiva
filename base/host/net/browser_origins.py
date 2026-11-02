# meiva:note
# The same-origin browser entries of a cluster, as a value. `MEIVA_BROWSER_ORIGINS` is a
# comma-separated list of `https://host[:port]` origins, each a reverse proxy that serves the
# frontend and the gateway under one host (`/api` to the gateway, everything else to Gate). It is
# parsed here once, at the boundary, into canonical origins, and every "is this page one of our
# entries?" question is asked of `is_browser_origin_url`. Stdlib only, so it is safe to import
# during the Settings load (base.config) and from base.cluster. The Gate login page
# (`services/entrypoints/gate/static/login.html`) and the frontend
# (`ui/web/src/lib/transport/gateway-origin.ts`) repeat the membership test over the same list.
from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from urllib.parse import SplitResult, urlsplit

__all__ = [
    "browser_origin_of",
    "is_browser_origin_url",
    "parse_browser_origins",
    "primary_browser_origin",
    "split_browser_origins",
]

_DEFAULT_PORTS = {"http": 80, "https": 443}
_DNS_NAME = re.compile(r"[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?\Z")


# meiva:note
# The entries of a comma-separated list; the empty (or blank) text is the empty list. An empty
# entry inside a non-empty list stays an entry, and `parse_browser_origins` refuses it.
def split_browser_origins(raw: str) -> list[str]:
    return raw.split(",") if raw.strip() else []


# meiva:note
# `scheme://host[:port]` for the parts of a URL: host lowercased by `urlsplit`, an IPv6 literal
# bracketed again, the scheme's default port left out — the form a browser serializes.
def _origin(scheme: str, host: str, port: int | None) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    suffix = "" if port is None or port == _DEFAULT_PORTS.get(scheme) else f":{port}"
    return f"{scheme}://{bracketed}{suffix}"


# meiva:note
# The canonical origin of an absolute URL, or None when it names no scheme or host.
def browser_origin_of(url: str) -> str | None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    return _origin(parts.scheme, parts.hostname, port)


# meiva:note
# Whether `host` is a DNS name or an IPv6 literal (unbracketed, as `urlsplit` returns it). The check
# also keeps a configured value safe to embed in the login page.
def _is_host(host: str) -> bool:
    if ":" not in host:
        return _DNS_NAME.match(host) is not None
    try:
        ipaddress.IPv6Address(host)
    except ValueError:
        return False
    return True


# meiva:note
# Whether the text carries only the characters of an origin: ASCII, no whitespace, no query or
# fragment marker. Checked on the raw text because `urlsplit` silently drops some of them.
def _is_origin_text(text: str) -> bool:
    return (
        text.isascii() and not any(char.isspace() for char in text) and not {"?", "#"} & set(text)
    )


# meiva:note
# Whether a parsed URL has an authority and nothing after it: no credentials, no empty port, no path
# but a lone slash.
def _is_bare_authority(parts: SplitResult) -> bool:
    return "@" not in parts.netloc and not parts.netloc.endswith(":") and parts.path in ("", "/")


# meiva:note
# One list entry as a canonical origin. Anything that is not a plain HTTPS origin is refused, not
# repaired: a path, query, fragment, credentials, another scheme, an empty port, whitespace or a
# host that is not a DNS name or IPv6 literal. A lone trailing slash and a default port are
# formatting, so the canonical form drops them.
def _canonical_origin(entry: str) -> str:
    text = entry.strip()
    problem = ValueError(f"browser origin {entry!r} must be an https origin (https://host[:port])")
    if not _is_origin_text(text):
        raise problem
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError as exc:
        raise problem from exc
    host = parts.hostname
    if parts.scheme != "https" or not host or not _is_bare_authority(parts) or not _is_host(host):
        raise problem
    return _origin(parts.scheme, host, port)


# meiva:note
# The canonical origins of the configured entries, in the order written. An invalid entry or a
# repeated origin raises `ValueError` (the process refuses to start on it); nothing is repaired.
def parse_browser_origins(entries: Sequence[str]) -> tuple[str, ...]:
    origins = tuple(_canonical_origin(entry) for entry in entries)
    if len(set(origins)) != len(origins):
        raise ValueError(f"browser origins must not repeat an origin: {list(origins)}")
    return origins


# meiva:note
# The entry that stands for the cluster where one address is needed (the login link `meiva start`
# prints): the first one configured, or None when no entry is configured.
def primary_browser_origin(origins: Sequence[str]) -> str | None:
    return origins[0] if origins else None


# meiva:note
# Whether `url` (a request URL or a page address) is on one of the entries: same scheme, host and
# port, the scheme's default port counting as absent.
def is_browser_origin_url(url: str, origins: Sequence[str]) -> bool:
    origin = browser_origin_of(url)
    return origin is not None and origin in origins
