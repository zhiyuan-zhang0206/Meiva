# meiva:note
# Reading a parsed message, as values: the List-Id and date headers, the metadata every lens
# returns, address lists, and the body text (text/plain preferred, html stripped as the
# fallback).

from __future__ import annotations

import datetime as _dt
import email.utils
import html
import re
from email.message import EmailMessage, Message

from base.host.boundary.untyped import object_of

_RE_TAG = re.compile(r"<[^>]+>")
_RE_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_RE_WS = re.compile(r"[ \t]*\n[ \t]*")
_RE_LIST_ID_BRACKET = re.compile(r"<([^>]+)>")


# meiva:note
# The List-Id value: the `<...>` bracket content (`Name <id.domain>` or bare `<id.domain>`),
# else the trimmed string. None when the header is absent. RFC 2919: `[description]
# <list-id>` -- the id is the LAST bracket pair (the optional human description precedes it
# and may itself contain `<...>`), and the id is a dot-atom that never contains brackets, so
# the last pair is it.
def parse_list_id(raw: str | None) -> str | None:
    if not raw:
        return None
    # meiva:note
    # `findall` answers `list[Any]` whatever the pattern is, so the last bracket pair is taken off
    # `finditer` with each capture guarded into a `str`.
    last: str | None = None
    for match in _RE_LIST_ID_BRACKET.finditer(raw):
        bracket = match.group(1)
        if isinstance(bracket, str):
            last = bracket
    return last.strip() if last is not None else raw.strip()


# meiva:note
# A Date header as a UTC ISO-8601 string ending in Z; a header without a zone is read as
# UTC. None for an absent or unparseable header.
def iso(date_hdr: str | None) -> str | None:
    if not date_hdr:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(date_hdr)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.UTC)
    return dt.astimezone(_dt.UTC).isoformat().replace("+00:00", "Z")


# meiva:note
# The value of header `name`, or None. `Message.__getitem__` answers `Any` (a header may be a
# structured object, not text), and `reportAny` leaves `(value or "")` alone because the union
# carries the `Any`; `object_of` reads it as an opaque value and the guard makes it the `str` a
# plain header is. Every header read in this skill goes through here.
def header(msg: Message[str, str], name: str) -> str | None:
    value = object_of(msg.__getitem__, name)
    return value if isinstance(value, str) else None


# meiva:note
# The metadata every lens returns -- newsletter identity lives in List-Id.
def meta(msg: Message[str, str]) -> dict[str, str | None]:
    return {
        "message_id": (header(msg, "Message-Id") or "").strip().strip("<>") or None,
        "list_id": parse_list_id(header(msg, "List-Id")),
        "from": (header(msg, "From") or "").strip() or None,
        "subject": (header(msg, "Subject") or "").strip() or None,
        "date": iso(header(msg, "Date")),
    }


# meiva:note
# Parse a comma-separated address header into bare addresses (RFC 5322 uses commas;
# `getaddresses` does not split on `;`).
def addr_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [addr for _, addr in email.utils.getaddresses([raw]) if addr]


def _html_to_text(html_body: str) -> str:
    no_scripts = _RE_SCRIPT_STYLE.sub(" ", html_body)
    text = html.unescape(_RE_TAG.sub(" ", no_scripts))
    return _RE_WS.sub("\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


# meiva:note
# The issue body as text: the text/plain MIME part when present (already close to clean
# markdown for Substack/Ghost/Buttondown senders), else the text/html part stripped to text.
# Newsletters are heavily-templated MIME, so plain-text-first skips most chrome for free.
def body_text(msg: EmailMessage) -> str:
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        # meiva:note
        # `get_content` answers `Any` (the part's payload may be bytes, a message or text), so it is
        # read as an opaque value and rendered by `str` -- the text this function returns.
        return str(object_of(plain.get_content)).strip()
    htmlpart = msg.get_body(preferencelist=("html",))
    if htmlpart is not None:
        return _html_to_text(str(object_of(htmlpart.get_content)))
    return ""
