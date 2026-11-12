# meiva:note
# The pure half of restoring an agent's selected page after a daemon restart: telling which
# Chrome page target is which upstream page.

from __future__ import annotations

from collections.abc import Mapping


# meiva:note
# The one Chrome page target that is the one upstream page ``page_id`` of ``listing``
# (page id -> URL), or None. The upstream's listing carries no target ids, so the URL is the
# only bridge, and it is one only when exactly one tab on each side shows it: with two tabs
# on a URL nothing says which is which, and a wrong guess would select (and later act on)
# the wrong tab. ``pages``: target id -> URL.
def target_of_listed_page(
    listing: Mapping[int, str], pages: Mapping[str, str], page_id: int
) -> str | None:
    url = listing.get(page_id)
    if url is None or sum(1 for u in listing.values() if u == url) != 1:
        return None
    targets = [t for t, u in pages.items() if u == url]
    return targets[0] if len(targets) == 1 else None


# meiva:note
# The inverse of ``target_of_listed_page``: the upstream page that is ``target_id``, with the
# same one-tab-per-URL condition on both sides.
def listed_page_of_target(
    listing: Mapping[int, str], pages: Mapping[str, str], target_id: str
) -> int | None:
    url = pages.get(target_id)
    if url is None or sum(1 for u in pages.values() if u == url) != 1:
        return None
    ids = [i for i, u in listing.items() if u == url]
    return ids[0] if len(ids) == 1 else None
