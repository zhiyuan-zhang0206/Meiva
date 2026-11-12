from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from base.host.boundary.json_values import is_object_dict
from base.lm.harness.codex.frames import INVALID_PARAMS, JsonRpcError, TransportClosedError
from base.lm.harness.codex.reconciliation.rollout import codex_home as resolve_codex_home
from base.lm.harness.codex.transcript.history import (
    PAGE_ITEMS,
    TAIL_ITEMS,
    HistoryRead,
    MalformedPageError,
    Page,
    forward_rows,
    item_id,
    items_request,
    page_of,
    tail_rows,
)
from base.lm.harness.codex.transcript.link import History, TranscriptLink
from base.lm.harness.codex.transcript.parse import parse_item_row, parse_stored_row
from base.lm.harness.codex.transcript.position import CodexPosition, HistoryMark, decode, encode
from base.lm.harness.codex.transcript.rollout_tail import RolloutFile, RolloutRead
from base.lm.harness.codex.transcript.shapes import Obj
from base.lm.harness.codex.transcript.store_filter import stored_item_row, stored_rollout_line
from base.lm.harness.session.jsonl.position import BAD_DEPTH, SKIPPED, Position
from base.lm.harness.session.jsonl.store_text import scrub
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    Anomaly,
    AnomalyKind,
    AnyEntry,
    Order,
    SubagentLink,
)
from base.lm.harness.session.transcript.ports import EntryBatch, PositionLostError, StoredRow

# meiva:note
# The one reader of a Codex session's own record: it implements `RecordReader` by reading a thread's item history
# after the stored cursor (`thread/items/list`, over the session's own app-server connection, `link.py`) and the few
# rollout lines the history lacks (`rollout_tail.py`), whatever woke the writer. The main record is the session's
# thread; a child thread a `spawnAgent` call started is a record of its own, named by its thread id. It never
# asks for a thread's whole history (no `thread/read`, no `thread/resume` with turns): every request is a page of
# at most `PAGE_ITEMS` items after a cursor or the newest `TAIL_ITEMS` (see `history.py` for how a read decides
# between the two), so a request, its answer and the work after it do not grow with the length of the thread.
#
# Which records are read. The main thread always; a child thread from the moment a notification named it or a
# `spawnAgent` call linked it, until a read of it brings nothing and no notification has named it since. So a
# conversation with many finished children costs a request for the main record and one for each child that is
# still working. A read that has nothing to bring costs no request at all while the rollout file has not grown, no
# notification has named the thread and the last request is recent (`QUIET_RECHECK_SECONDS`): the poll of an idle
# agent is a `stat`.
#
# What one read returns. Within a record, the history items first, then (only when the items reached the end of the
# history, so a turn's end is never read before its last item) the rollout lines read before the items were asked
# for, which gives a turn's end, a failure, an abort, a sample's token usage and a finished child's report. A child
# record takes its token usage from the rollout only: a child's own turn boundaries are not its parent's. The rollout
# is read first and the history second, so every item the rollout lines refer to is in the answer. A page that
# the history truncates is returned as it is and the rollout waits for a later read.
#
# The memory it keeps is the set of turns whose first input was read, per record and position (a steer is told from
# the input that opened its turn by it), never advanced past what the caller holds, so a batch whose database write
# failed is read again with the state it had the first time. A restart loses the state; the writer's rebuild gives it
# back through `parse_stored`.
# meiva:link base/lm/harness/docs/transcript-projection.md

REQUEST_TIMEOUT_SECONDS = 30.0
QUIET_RECHECK_SECONDS = 30.0
MAX_ROLLOUT_BYTES = 4 * 1024 * 1024
MAX_ROLLOUT_ROW_BYTES = 64 * 1024 * 1024
SEED_STEPS = 128
_REMEMBERED_POSITIONS = 2
_STATES_KEPT = 64
_REPORTED_KEPT = 1024


@dataclass(slots=True)
class _Quiet:
    key: tuple[str, tuple[int, int] | None, int]
    at: float


class CodexRecordReader:
    def __init__(
        self,
        *,
        codex_home: Path,
        thread_id: Callable[[], str | None],
        link: TranscriptLink,
        now: Callable[[], float],
    ) -> None:
        self._home = codex_home
        self._thread = thread_id
        self._link = link
        self._now = now
        self._files: dict[str, RolloutFile] = {}
        self._known: dict[str, None] = {}
        self._active: dict[str, None] = {}
        self._opened: dict[str, dict[str, set[str]]] = {}
        self._stored_opened: dict[str, set[str]] = {}
        self._quiet: dict[str, _Quiet] = {}
        self._reported: dict[str, None] = {}
        self._seen_activity: dict[str, int] = {}
        self.requests = 0
        self.items_read = 0
        self.rollout_bytes_read = 0

    @classmethod
    def for_session(
        cls,
        *,
        environ: Mapping[str, str],
        thread_id: Callable[[], str | None],
        link: TranscriptLink,
        now: Callable[[], float],
    ) -> CodexRecordReader:
        return cls(codex_home=resolve_codex_home(environ), thread_id=thread_id, link=link, now=now)

    def wire_id(self, key: str) -> str:
        return key

    def parse_stored(self, rows: Sequence[StoredRow]) -> tuple[AnyEntry, ...]:
        entries: list[AnyEntry] = []
        for stored in rows:
            opened = self._stored_opened.setdefault(stored.record, set())
            parsed = parse_stored_row(dict(stored.row), stored.ordinal, stored.record, opened)
            self._learn(parsed)
            entries.extend(parsed)
        return tuple(entries)

    def _learn(self, entries: Sequence[AnyEntry]) -> None:
        for entry in entries:
            if isinstance(entry, SubagentLink):
                self._known.setdefault(entry.child_record)
                self._active.setdefault(entry.child_record)

    # meiva:note
    # The records to read now: the main thread, then the children that are working. A child is working from the
    # moment a notification names it (the thread id is in the notification's `threadId`) or a spawn call links it,
    # until a read of it brings nothing. `exhaustive` (the last read before a close) lists every child this reader
    # has heard of.
    async def records(self, *, exhaustive: bool = False) -> tuple[str, ...]:
        main = self._thread()
        for thread, count in self._link.activity().items():
            if thread != main and self._seen_activity.get(thread) != count:
                self._known.setdefault(thread)
                self._active.setdefault(thread)
        children = self._known if exhaustive else self._active
        return (MAIN_RECORD, *sorted(children))

    async def read_after(self, record: str, position: str | None) -> EntryBatch | None:
        thread = self._thread_of(record)
        history = self._link.history()
        if thread is None or history is None:
            return None
        try:
            batch = await self._read(record, thread, history, position)
        except JsonRpcError as error:
            if error.code == INVALID_PARAMS:
                raise PositionLostError(
                    f"the app-server refused the stored cursor: {error}"
                ) from error
            if record == MAIN_RECORD:
                raise
            return self._drop_child(record, f"app-server: {error.message}")
        except TransportClosedError:
            return None
        if batch is None and record != MAIN_RECORD:
            self._retire(record, thread)
        return batch

    def _thread_of(self, record: str) -> str | None:
        return self._thread() if record == MAIN_RECORD else record

    def _retire(self, record: str, thread: str) -> None:
        quiet = self._quiet.get(record)
        stamp = self._link.activity().get(thread, 0)
        if quiet is not None and quiet.key[2] == stamp:
            _ = self._active.pop(record, None)
            self._seen_activity[thread] = stamp

    def _drop_child(self, record: str, detail: str) -> EntryBatch | None:
        _ = self._active.pop(record, None)
        _ = self._known.pop(record, None)
        return self._anomaly_batch(record, f"child thread unreadable: {detail}")

    def _anomaly_batch(
        self, record: str, detail: str, position: str | None = None
    ) -> EntryBatch | None:
        if detail in self._reported:
            return None
        self._reported[detail] = None
        while len(self._reported) > _REPORTED_KEPT:
            del self._reported[next(iter(self._reported))]
        entry = Anomaly(
            key=f"codex:anomaly:{record}:{detail}",
            order=Order(0),
            record=record,
            anomaly=AnomalyKind.BAD_ROW,
            detail=detail,
        )
        return EntryBatch(record, (entry,), position or encode(CodexPosition()), (), 0)

    async def _request(self, history: History, method: str, params: Mapping[str, object]) -> Obj:
        self.requests += 1
        return await history.request(method, params, timeout=REQUEST_TIMEOUT_SECONDS)

    async def _page(
        self, history: History, thread: str, *, limit: int, descending: bool, cursor: str | None
    ) -> Page:
        method, params = items_request(thread, limit=limit, descending=descending, cursor=cursor)
        return page_of(await self._request(history, method, params))

    async def _read(
        self, record: str, thread: str, history: History, stored: str | None
    ) -> EntryBatch | None:
        position = CodexPosition() if stored is None else decode(stored)
        file = self._file(thread, record)
        key = (stored or "", file.stamp() if file is not None else None, self._activity(thread))
        if self._is_quiet(record, key):
            return None
        snapshot = (
            self._read_rollout(file, position, record)
            if file is not None and not position.behind
            else None
        )
        try:
            read = await self._history(history, thread, position.history)
        except MalformedPageError as error:
            return self._anomaly_batch(record, f"history: {error}", stored)
        chosen = _chosen(read, position, snapshot)
        if chosen is None:
            self._quiet[record] = _Quiet(key, self._now())
            return None
        return self._assemble(record, position, stored, *chosen)

    def _activity(self, thread: str) -> int:
        return self._link.activity().get(thread, 0)

    def _is_quiet(self, record: str, key: tuple[str, tuple[int, int] | None, int]) -> bool:
        quiet = self._quiet.get(record)
        return (
            quiet is not None
            and quiet.key == key
            and self._now() - quiet.at < QUIET_RECHECK_SECONDS
        )

    def _file(self, thread: str, record: str) -> RolloutFile | None:
        known = self._files.get(thread)
        if known is not None:
            return known
        hint = self._link.rollout_hint if record == MAIN_RECORD else None
        found = RolloutFile.locate(self._home, thread, hint=hint)
        if found is not None:
            self._files[thread] = found
        return found

    def _read_rollout(
        self, file: RolloutFile, position: CodexPosition, record: str
    ) -> RolloutRead | None:
        try:
            read = file.read(
                position.rollout,
                max_batch=MAX_ROLLOUT_BYTES,
                max_row=MAX_ROLLOUT_ROW_BYTES,
                main=record == MAIN_RECORD,
            )
        except FileNotFoundError:
            return None
        self.rollout_bytes_read += read.bytes_read
        return read

    # meiva:note
    # An up-to-date position costs one request: the newest few items hold the last item read and what follows it is
    # new. A position that is behind (a cursor after the last item of a page, or an inclusive one whose last item is
    # not among the newest few) reads the next page after its cursor; a page that does not reach the end is the
    # batch, one that does is read again as the newest items (large enough to hold it) so that the position after it
    # is a cursor at an item (`history.py`).
    async def _history(
        self, history: History, thread: str, mark: HistoryMark
    ) -> HistoryRead | None:
        if mark.inclusive:
            tail = await self._tail(history, thread, mark, TAIL_ITEMS)
            if tail is not None:
                return tail
        page = await self._page(
            history, thread, limit=PAGE_ITEMS, descending=False, cursor=mark.cursor
        )
        forward = forward_rows(page, mark)
        if not forward.final:
            return forward
        if forward.rows:
            return await self._tail(history, thread, mark, len(forward.rows) + TAIL_ITEMS)
        if mark.fresh or mark.inclusive:
            return forward
        return await self._tail(history, thread, mark, TAIL_ITEMS)

    async def _tail(
        self, history: History, thread: str, mark: HistoryMark, limit: int
    ) -> HistoryRead | None:
        page = await self._page(history, thread, limit=limit, descending=True, cursor=None)
        return tail_rows(page, mark)

    def _assemble(
        self,
        record: str,
        position: CodexPosition,
        stored: str | None,
        read: HistoryRead,
        snapshot: RolloutRead | None,
    ) -> EntryBatch:
        cache = self._opened.pop(record, {})
        self._opened[record] = cache
        while len(self._opened) > _STATES_KEPT:
            del self._opened[next(iter(self._opened))]
        opened = set(cache.get(stored or "", set()))
        subagent = record != MAIN_RECORD
        entries: list[AnyEntry] = []
        kept: list[StoredRow] = []
        ordinal = position.n
        for raw in read.rows:
            _ = scrub(raw)
            self.items_read += 1
            try:
                parsed = parse_item_row(raw, ordinal, record, opened)
                filtered = stored_item_row(raw, subagent=subagent)
            except RecursionError:
                parsed = [_bad_row(record, ordinal, BAD_DEPTH)]
                filtered = None
            entries.extend(parsed)
            if filtered is not None:
                kept.append(StoredRow(record, ordinal, filtered))
            ordinal += 1
        rollout = position.rollout
        if snapshot is not None:
            rollout = snapshot.moved
            for line in snapshot.rows:
                if line.row is None:
                    if line.bad != SKIPPED:
                        entries.append(_bad_row(record, ordinal, line.bad))
                        ordinal += 1
                    continue
                entries_and_row = self._rollout_line(record, ordinal, dict(line.row), opened)
                entries.extend(entries_and_row[0])
                if entries_and_row[1] is not None:
                    kept.append(StoredRow(record, ordinal, entries_and_row[1]))
                ordinal += 1
        self._learn(entries)
        moved = encode(CodexPosition(n=ordinal, history=read.mark, rollout=rollout))
        cache[moved] = opened
        while len(cache) > _REMEMBERED_POSITIONS:
            del cache[next(iter(cache))]
        size = sum(len(json.dumps(row.row, separators=(",", ":"))) for row in kept)
        return EntryBatch(record, tuple(entries), moved, tuple(kept), size)

    def _rollout_line(
        self, record: str, ordinal: int, row: dict[str, object], opened: set[str]
    ) -> tuple[list[AnyEntry], dict[str, object] | None]:
        _ = scrub(row)
        try:
            filtered = stored_rollout_line(row)
            if filtered is None:
                return [], None
            return parse_stored_row(filtered, ordinal, record, opened), filtered
        except RecursionError:
            return [_bad_row(record, ordinal, BAD_DEPTH)], None

    async def position_before(
        self, record: str, wire_ids: Sequence[str], *, after_ordinal: int = -1
    ) -> str | None:
        thread = self._thread_of(record)
        history = self._link.history()
        if thread is None or history is None:
            return None
        start = after_ordinal + 1
        mark = await self._seed_mark(history, thread, tuple(wire_ids))
        if mark is None:
            return None
        file = self._file(thread, record)
        rollout = await asyncio.to_thread(self._rollout_start, file, tuple(wire_ids))
        return encode(CodexPosition(n=start, history=mark, rollout=rollout))

    def _rollout_start(self, file: RolloutFile | None, wire_ids: tuple[str, ...]) -> Position:
        if file is None:
            return Position(0, 0, 0, "")
        read = file.start_before(wire_ids, max_row=MAX_ROLLOUT_ROW_BYTES)
        self.rollout_bytes_read += read.bytes_read
        return read.position

    # meiva:note
    # Where to start a record that already holds history: at the oldest item that carries one of our client ids (the
    # first input this process sent), found by walking the history backwards one item at a time from the newest, so
    # each step is a cursor AT that item; at the newest item when none is found within `SEED_STEPS` items; None for a
    # thread with no item. The walk stops at the first item older than every id still being looked for.
    async def _seed_mark(
        self, history: History, thread: str, wire_ids: tuple[str, ...]
    ) -> HistoryMark | None:
        wanted = set(wire_ids)
        newest: HistoryMark | None = None
        oldest_match: HistoryMark | None = None
        cursor: str | None = None
        for _ in range(SEED_STEPS):
            page = await self._page(history, thread, limit=1, descending=True, cursor=cursor)
            if not page.rows or page.back_cursor is None:
                break
            row = page.rows[0]
            here = HistoryMark(cursor=page.back_cursor, inclusive=True, last=item_id(row))
            newest = newest or here
            item = row.get("item")
            client = item.get("clientId") if is_object_dict(item) else None
            if wanted and isinstance(client, str) and client in wanted:
                oldest_match = replace(here, emit_first=True)
                wanted.discard(client)
                if not wanted:
                    break
            if not wire_ids or page.next_cursor is None:
                break
            cursor = page.next_cursor
        return oldest_match or newest


# meiva:note
# What a read amounts to once the history and the rollout snapshot are in: nothing (the position is up to date and
# the rollout brought no line the reader wants), the rollout lines alone (the history had nothing new), or the
# history page. A page that does not reach the end of the history is returned alone: the rollout lines wait for a
# read that does, so a turn's end is never read before its last item.
def _chosen(
    read: HistoryRead | None, position: CodexPosition, snapshot: RolloutRead | None
) -> tuple[HistoryRead, RolloutRead | None] | None:
    if read is not None and (read.rows or read.mark != position.history):
        return read, (snapshot if read.final else None)
    if snapshot is None or not snapshot.rows:
        return None
    return HistoryRead((), position.history, final=True), snapshot


def _bad_row(record: str, ordinal: int, reason: str | None) -> Anomaly:
    detail = reason or "unusable"
    return Anomaly(
        key=f"codex:anomaly:bad-row:{record}:{ordinal}",
        order=Order(ordinal),
        record=record,
        anomaly=AnomalyKind.BAD_ROW,
        detail=detail,
    )
