# meiva:note
# What a data-plane URL says about itself, and how it may be shown: stdlib only, so it is safe to
# import during the Settings load (base.config) and from base.cluster. The rewrites that change a
# URL's identity or address live in url_secret.py; nothing here mutates a URL.
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

from base.host.net.predicates import is_loopback_host

__all__ = [
    "names_foreign_host",
    "redacted_url",
    "url_has_query_param",
    "url_host",
    "url_username",
]


# meiva:note
# The host a URL names (`urlsplit(...).hostname`, lowercased, an IPv6 literal unbracketed) or
# `fallback` when it carries none. The data-plane URLs this codebase writes always carry a host (the
# boot sentinels name loopback), so the fallback is a floor for a hand-written URL — and never a route
# for an empty host to reach something unintended: 127.0.0.1 is unreachable off-box and the connect
# guard still matches the placeholder URL byte-for-byte.
def url_host(url: str, fallback: str = "127.0.0.1") -> str:
    return urlsplit(url).hostname or fallback


# meiva:note
# The username a URL carries, or empty when absent.
def url_username(url: str) -> str:
    return urlsplit(url).username or ""


# meiva:note
# Whether `url`'s query string carries a non-blank `key` parameter.
def url_has_query_param(url: str, key: str) -> bool:
    return key in parse_qs(urlsplit(url).query)


# meiva:note
# Whether any of `urls` dials a host other than this box. The one owner of the "is this data plane
# off-box" fact: the configuration's `is_remote` and the commands' config value both answer here, so
# a URL swapped on a copy of the config moves the answer with it. A host-less URL reads as loopback
# (`url_host`'s fallback), so the placeholder and unix-socket forms are local.
def names_foreign_host(*urls: str) -> bool:
    return any(not is_loopback_host(url_host(url)) for url in urls)


_USERINFO_PASSWORD_RE = re.compile(r"(://[^/@]*:)[^/@]*(@)")


# meiva:note
# `url` with any userinfo password replaced by `***` — safe to embed in exceptions and log lines.
# Operates on the raw text, not `urlsplit`, so a value the parser would not fully decode (an
# outer-quoted .env line, an unparseable host) still loses its `scheme://user:secret@` password
# before reaching a log. The username is names-as-data identity, never secret, and stays visible; a
# URL with no password comes back unchanged.
def redacted_url(url: str) -> str:
    return _USERINFO_PASSWORD_RE.sub(r"\1***\2", url)
