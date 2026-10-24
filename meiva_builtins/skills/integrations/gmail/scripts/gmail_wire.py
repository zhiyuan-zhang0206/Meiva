# meiva:note
# How the gmail skill reads Gmail's IMAP answers, as values: quoting a Gmail query for
# X-GM-RAW, the sequence numbers a SEARCH answer names (and the way Gmail signals a refused
# query), the newest-first window of them, and the Drafts folder in a LIST answer. Sending
# commands and reading the socket live in imap.py.

from __future__ import annotations

import re
from collections.abc import Sequence

from gmail_auth import GmailError

# meiva:note
# Gmail's "All Mail" holds archived issues too, not just the inbox -- a newsletter the user
# archived is still a feed item, so enum/search run against All Mail.
ALL_MAIL = '"[Gmail]/All Mail"'

_RE_LIST_LINE = re.compile(r'\(([^)]*)\)\s+(?:"[^"]*"|NIL)\s+(.+)$')


# meiva:note
# Quote a Gmail-syntax query as an IMAP string for the X-GM-RAW criterion.
def xgm(query: str) -> str:
    return '"' + query.replace("\\", "\\\\").replace('"', '\\"') + '"'


# meiva:note
# The matching sequence numbers (oldest-first) of a Gmail X-GM-RAW SEARCH answer.
#
# Gmail signals a rejected query by answering the SEARCH with `[SERVERBUG] infeasible query`
# in the data line rather than a BAD status, so a non-OK status or an empty-but-error
# payload both raise instead of looking like a zero-result match.
def parse_search_response(typ: str, data: Sequence[bytes | None], query: str) -> list[bytes]:
    if typ != "OK":
        raise GmailError(f"search failed (typ={typ}) for {query!r}: {data!r}")
    payload = data[0] if data else b""
    if payload and payload.split()[0].startswith(b"["):
        raise GmailError(f"Gmail refused the search {query!r}: {payload.decode(errors='replace')}")
    return payload.split() if payload else []


# meiva:note
# The newest `n` ids, newest-first (IMAP returns ascending sequence order). A negative `n` is
# a caller bug (it would slice off the *oldest* `|n|` and silently return a wrong-length
# list), so it is rejected rather than mis-sliced.
def recent(ids: list[bytes], n: int | None) -> list[bytes]:
    if n is not None and n < 0:
        raise ValueError(f"limit/scan must be >= 0, got {n}")
    newest_first = ids[::-1]
    return newest_first[:n] if n is not None else newest_first


# meiva:note
# The mailbox's drafts folder name in a LIST answer, found via its RFC 6154 `\Drafts`
# special-use attribute (the display name is localized per account; the attribute is not).
# Raises when a line does not parse or the server advertises no such folder.
def parse_drafts_folder(lines: Sequence[object]) -> str:
    for line in lines:
        s = line.decode() if isinstance(line, bytes) else str(line)
        m = _RE_LIST_LINE.match(s)
        if not m:
            raise GmailError(f"unparseable LIST line: {s!r}")
        attrs, name = m.groups()
        if "\\drafts" in attrs.lower():
            name = name.strip()
            return name[1:-1] if name.startswith('"') and name.endswith('"') else name
    raise GmailError("the server advertises no \\Drafts special-use folder")
