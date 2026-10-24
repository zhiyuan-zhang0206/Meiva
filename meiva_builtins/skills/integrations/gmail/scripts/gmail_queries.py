# meiva:note
# The Gmail searches each lens runs and how their results are summarised, as values: the
# `--since` window, the query for a Message-Id / a free query / a newsletter / the discovery
# net, and the per-newsletter count of what discovery saw. Running a search lives in imap.py.

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Iterable, Mapping

# meiva:note
# Coarse discovery net (recall-broad, precision-loose): Gmail's own bulk tabs. `discover`
# widens to these then keeps only List-Id-bearing messages, because Gmail exposes no
# server-side "has a List-Id header" filter (a bare `list:*` wildcard does not filter, and
# IMAP `HEADER List-Id ""` is rejected as infeasible) -- the List-Id confirm is always
# client-side after the fetch.
DISCOVER_NET = "category:updates OR category:forums"

_RE_RELATIVE_SINCE = re.compile(r"(\d+)([dh])")


# meiva:note
# `after:<epoch>` clause for a unix-seconds cutoff (Gmail accepts epoch).
def _after(since_ts: int | None) -> str:
    return f" after:{since_ts}" if since_ts is not None else ""


# meiva:note
# Parse `--since` to a unix-seconds cutoff as of `now_ts`: `30d` / `12h` / ISO `2026-05-07`
# (a date without a zone is read as UTC). None when there is no bound.
def since_to_ts(since: str | None, now_ts: int) -> int | None:
    if since is None:
        return None
    m = _RE_RELATIVE_SINCE.fullmatch(since)
    if m:
        secs = int(m.group(1)) * (86400 if m.group(2) == "d" else 3600)
        return now_ts - secs
    try:
        parsed = _dt.datetime.fromisoformat(since)
    except ValueError:
        raise ValueError(f"--since must be like 30d, 12h or 2026-05-07, got {since!r}") from None
    parsed = parsed.replace(tzinfo=_dt.UTC) if parsed.tzinfo is None else parsed.astimezone(_dt.UTC)
    return int(parsed.timestamp())


# meiva:note
# The query that finds one message by its RFC822 Message-Id (`rfc822msgid:`), accepting the
# id with or without its angle brackets.
def message_query(message_id: str) -> str:
    return f"rfc822msgid:{message_id.strip().strip('<>')}"


# meiva:note
# The query of the read lens: exactly one selector, either a Gmail search or a Message-Id.
def read_query(query: str | None, message_id: str | None) -> str:
    if query is not None and message_id is None:
        return query
    if query is None and message_id is not None:
        return message_query(message_id)
    raise ValueError("read needs exactly one of query / message_id")


# meiva:note
# The query of the discover lens. The disjunction is parenthesized so `after:` binds the
# whole net -- unparenthesized, Gmail's `OR` binds tighter than the implicit space-AND,
# leaving the `updates` arm unbounded by --since.
def discover_query(since_ts: int | None) -> str:
    return f"({DISCOVER_NET}){_after(since_ts)}"


# meiva:note
# The query of the enum lens: one newsletter's issues (`list:` filters server-side, so every
# result already belongs to this List-Id), bounded by `since_ts`.
def enum_query(list_id: str, since_ts: int | None) -> str:
    return f"list:{list_id}{_after(since_ts)}"


# meiva:note
# The distinct newsletter List-Ids among `metas`, as `{list_id, count, sample_from,
# sample_subject}` rows sorted by count (the sample is the first message seen). Messages
# without a List-Id are not newsletters and are dropped.
def list_id_counts(metas: Iterable[Mapping[str, str | None]]) -> list[dict[str, str | int | None]]:
    agg: dict[str, dict[str, str | int | None]] = {}
    for m in metas:
        lid = m["list_id"]
        if not lid:
            continue
        row = agg.setdefault(
            lid,
            {"list_id": lid, "count": 0, "sample_from": m["from"], "sample_subject": m["subject"]},
        )
        row["count"] = int(row["count"] or 0) + 1
    return sorted(agg.values(), key=lambda r: int(r["count"] or 0), reverse=True)
