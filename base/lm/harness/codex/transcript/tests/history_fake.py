from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness.codex.frames import INVALID_PARAMS, JsonRpcError
from base.lm.harness.codex.transcript.shapes import Obj

# meiva:note
# A thread's item history the way the app-server serves it over `thread/items/list`, for the reader's tests: each
# row has the ordinal of the rollout line it was written at (rows are not consecutive), a page is ascending or
# descending with a `limit`, `nextCursor` is given when the page was cut (positioned after the last item returned,
# in that direction) and `backwardsCursor` is positioned AT the page's first item, inclusive, exactly as the
# recordings show (`s1-items-end.json`, `p1`: an ascending cursor with `includeAnchor` false yields the items
# after its ordinal; one with `includeAnchor` true yields the item itself first). The cursor is a JSON string
# naming the thread and the ordinal, so a test can corrupt one. Every call is kept in `calls`.


class FakeHistory:
    def __init__(self, thread: str, *, step: int = 2) -> None:
        self.thread = thread
        self.rows: list[tuple[int, Obj]] = []
        self.calls: list[tuple[str, Obj]] = []
        self._ordinal = 10
        self._step = step

    def append(self, row: Obj) -> None:
        self.rows.append((self._ordinal, copy.deepcopy(row)))
        self._ordinal += self._step

    def remove(self, item_id: str) -> None:
        self.rows = [(o, r) for o, r in self.rows if _item(r)["id"] != item_id]

    @property
    def methods(self) -> list[str]:
        return [method for method, _ in self.calls]

    async def request(
        self, method: str, params: Mapping[str, object], *, timeout: float | None = None
    ) -> Obj:
        del timeout
        stored = dict(params)
        self.calls.append((method, stored))
        assert method == "thread/items/list", method
        assert stored["threadId"] == self.thread
        descending = stored.get("sortDirection") == "desc"
        limit = int(str(stored.get("limit", 100)))
        selected = self._from(stored.get("cursor"), descending)
        page = selected[:limit]
        result: Obj = {
            "data": [copy.deepcopy(row) for _, row in page],
            "nextCursor": self._cursor(page[-1][0], inclusive=False)
            if len(selected) > limit
            else None,
            "backwardsCursor": self._cursor(page[0][0], inclusive=True) if page else None,
        }
        return result

    def _from(self, cursor: object, descending: bool) -> list[tuple[int, Obj]]:
        ordered = sorted(self.rows, key=lambda pair: pair[0], reverse=descending)
        if cursor is None:
            return ordered
        ordinal, inclusive = self._decode(cursor)
        found: list[tuple[int, Obj]] = []
        for pair in ordered:
            before = pair[0] < ordinal if descending else pair[0] > ordinal
            if before or (inclusive and pair[0] == ordinal):
                found.append(pair)
        return found

    def _decode(self, cursor: object) -> tuple[int, bool]:
        try:
            body = object_of(json.loads, str(cursor))
        except ValueError:
            raise JsonRpcError(INVALID_PARAMS, "bad cursor") from None
        if not is_object_dict(body) or body.get("requestedThreadId") != self.thread:
            raise JsonRpcError(INVALID_PARAMS, "bad cursor")
        ordinal, inclusive = body["rolloutOrdinal"], body["includeAnchor"]
        assert isinstance(ordinal, int) and isinstance(inclusive, bool)
        return ordinal, inclusive

    def _cursor(self, ordinal: int, *, inclusive: bool) -> str:
        return json.dumps(
            {
                "requestedThreadId": self.thread,
                "rolloutOrdinal": ordinal,
                "includeAnchor": inclusive,
                "scope": {"kind": "itemsByCreatedAtOrdinal"},
            },
            separators=(",", ":"),
        )


def _item(row: Obj) -> Obj:
    item = row["item"]
    assert is_object_dict(item)
    return item


def write_lines(path: Path, lines: Sequence[Obj]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for line in lines:
            _ = handle.write(json.dumps(line, separators=(",", ":")) + "\n")
