from __future__ import annotations

from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.codex.transcript.errors import MissingFieldError
from base.lm.harness.codex.transcript.position import HistoryMark
from base.lm.harness.codex.transcript.scalars import text
from base.lm.harness.codex.transcript.shapes import Obj, obj
from base.lm.harness.session.transcript.ports import PositionLostError

# meiva:note
# The decisions of reading a thread's item history by cursor, apart from the requests that fetch it. The history
# method is `thread/items/list`: its pages come ascending or descending, each with `nextCursor` (more pages in that
# direction; None when the page is the last) and `backwardsCursor` (a cursor positioned AT the page's first item,
# inclusive). Those cursors are opaque; this module only passes them on.
#
# A record is read in one of two ways. The tail read asks for the newest few items (descending) and looks for the
# last item read among them: when it is there, what is newer than it is exactly the new items, and the page's
# `backwardsCursor` is a cursor at the newest item, taken in one answer with the items (so no item can slip between
# the two). When the last item read is not among the newest few, or nothing has been read yet, the forward read
# asks for a page after the stored cursor (ascending); a page the history truncates leaves a cursor AFTER its last
# item and the next page continues from it. A forward page that reaches the end is not used as it is, for it has no
# cursor at its last item: the reader asks for the tail again, large enough to hold it (`tail_rows`), and takes
# that answer. A forward read from a cursor positioned AT the last item read checks the first item it yields: it
# must be that item, or the history no longer holds what was read and the position is lost.
# meiva:link base/lm/harness/docs/transcript-projection.md

PAGE_ITEMS = 32
TAIL_ITEMS = 8


class MalformedPageError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Page:
    rows: tuple[Obj, ...]
    next_cursor: str | None
    back_cursor: str | None


def item_id(row: Obj) -> str:
    return text(obj(row.get("item"), "item"), "id")


def items_request(
    thread: str, *, limit: int, descending: bool, cursor: str | None
) -> tuple[str, dict[str, object]]:
    params: dict[str, object] = {
        "threadId": thread,
        "limit": limit,
        "sortDirection": "desc" if descending else "asc",
    }
    if cursor is not None:
        params["cursor"] = cursor
    return "thread/items/list", params


def page_of(response: Obj) -> Page:
    data = response.get("data")
    if not is_object_list(data):
        raise MalformedPageError("thread/items/list answered without a data list")
    rows: list[Obj] = []
    for row in data:
        if not is_object_dict(row):
            raise MalformedPageError("thread/items/list answered a row that is not an object")
        try:
            _ = item_id(row)
        except MissingFieldError as error:
            raise MalformedPageError(f"a history row has no {error.field}") from None
        rows.append(row)
    return Page(tuple(rows), _cursor(response, "nextCursor"), _cursor(response, "backwardsCursor"))


def _cursor(response: Obj, field: str) -> str | None:
    value = response.get(field)
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True)
class HistoryRead:
    rows: tuple[Obj, ...]
    mark: HistoryMark
    final: bool


# meiva:note
# The new items out of a descending page, oldest first, with the mark after them; None when the page does not
# contain the last item read (or, for a record read from its start, does not hold the whole history).
def tail_rows(page: Page, mark: HistoryMark) -> HistoryRead | None:
    if page.rows and page.back_cursor is None:
        raise MalformedPageError("thread/items/list answered a page without a backwardsCursor")
    ids = [item_id(row) for row in page.rows]
    if mark.last is None:
        if page.next_cursor is not None:
            return None
        found = len(page.rows)
    elif mark.last in ids:
        found = ids.index(mark.last) + (1 if mark.emit_first else 0)
    else:
        return None
    new = tuple(reversed(page.rows[:found]))
    if not page.rows:
        return HistoryRead((), mark, final=True)
    after = HistoryMark(cursor=page.back_cursor, inclusive=True, last=ids[0])
    return HistoryRead(new, after, final=True)


# meiva:note
# The items out of an ascending page read from the stored cursor, oldest first, and the mark after them. A cursor
# positioned at the last item read yields that item first (it is dropped, or kept for a record seeded at it); a page
# that does not start with it means the history was rewritten. A page the history truncates (`nextCursor` given)
# leaves a cursor after its last item and is final=False; one that reaches the end is returned with the mark
# unchanged and final=True, to be replaced by a tail read.
def forward_rows(page: Page, mark: HistoryMark) -> HistoryRead:
    rows = list(page.rows)
    if mark.inclusive and mark.last is not None:
        if not rows or item_id(rows[0]) != mark.last:
            raise PositionLostError(
                f"the history no longer starts with the last item read ({mark.last})"
            )
        if not mark.emit_first:
            rows = rows[1:]
    if page.next_cursor is None:
        return HistoryRead(tuple(rows), mark, final=True)
    last = item_id(rows[-1]) if rows else mark.last
    return HistoryRead(
        tuple(rows), HistoryMark(cursor=page.next_cursor, inclusive=False, last=last), final=False
    )
