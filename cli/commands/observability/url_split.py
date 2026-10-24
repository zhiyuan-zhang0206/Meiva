# meiva:note
# Splits a raw URL into the value the plans under `plans/` decide on.
#
# A port that is not a number is refused here, at the boundary, with the parser's own error; the
# decisions never see a half-parsed URL.

from __future__ import annotations

from urllib.parse import urlsplit

from cli.commands.observability.plans.url_parts import UrlParts

__all__ = ["split_url"]


def split_url(raw: str) -> UrlParts:
    parts = urlsplit(raw)
    return UrlParts(
        scheme=parts.scheme,
        netloc=parts.netloc,
        hostname=parts.hostname,
        port=parts.port,
        username=parts.username,
        password=parts.password,
        path=parts.path,
        query=parts.query,
        fragment=parts.fragment,
    )
