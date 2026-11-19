from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path

from agent.graph.claim.harness_activity import HarnessTurnTimingTracker
from agent.graph.hooks.user_delivery import capture_keyed_items
from base.agents.context import MeivaContext
from base.agents.history.transcript_timeline.items import ReplyPolicy, format_tool_use
from base.agents.history.transcript_timeline.project import (
    Projected,
    ProjectedItem,
    RecordProjector,
)
from base.agents.history.transcript_timeline.record.harness import (
    entry_reader,
    input_wire_ids,
    record_file,
    record_location,
    reply_key,
    reply_key_prefix,
    session_file,
    unreadable_file,
    wire_id,
)
from base.agents.history.transcript_timeline.record.match import CLAIM_EARLY, CLAIM_SLACK
from base.agents.history.transcript_timeline.record.read import read_window, reasoning_refresh
from base.agents.history.transcript_timeline.sent import (
    DELIVERABLE_KINDS,
    load_sent_rows,
    sent_inputs,
)
from base.agents.messages.harness_input_key import recorded_input_key
from base.agents.messages.inbound import InboundKind
from base.agents.messages.reply_intent import parse_reply_visibility
from base.agents.to_user.delivery.store import has_keyed_publication
from base.api_contracts.agent_contract import Harness
from base.events.live.projection import InboundCommitted, TimelineSnapshot
from base.events.live.publisher import AgentEventPublisher
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.lm.harness import (
    AssistantText,
    BridgeEvent,
    HarnessSetupError,
    SubagentText,
    TextDelta,
    ToolResult,
    ToolUse,
)
from base.lm.harness.engine import HarnessTurn
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.session.transcript.entries import InputRow
from base.lm.harness.session.transcript.record_file import (
    BLOCK_BYTES,
    FileRow,
    RecordFile,
    UnreadRows,
)
from base.log import logger
from base.paths import harness_sessions_dir, workspace_dir

# meiva:note
# The timeline of a Claude Code, Codex or Antigravity agent's turn while it runs. The CLI's events say only that something new happened; what
# happened is read from the CLI's own record file, which is the one source of the timeline. A signal marks the sink
# dirty, and after `DEBOUNCE_SECONDS` the bytes appended since the last read are projected with the same function a
# cold read uses and published as a `TimelineSnapshot`: item ids are the rows' byte offsets, so a page that has read
# the same rows over HTTP merges the snapshot by id and a repeated publication changes nothing. A read costs the new
# bytes: the sink keeps, in memory only, the offset it has read to, the projector with the open turn and the calls
# that wait for a result, and the ids it has announced; none of it is stored, and a sink that starts over (a new
# process, a new invocation) begins at the start of the record's last turn, so what was shown before is shown again
# and left to the page to merge. Each snapshot carries the item published last as its first item (the page merges
# by id, and a snapshot that continues what the page holds is never read as a gap in it) and every call still
# running (a page drops a running item a snapshot does not mention).
# What the sink delivers: the replies of the items it projects, once each. A reply's publication key is the CLI's own id
# of the row that holds it (`harness:claude:<row uuid>`, `harness:codex:<message id>`), the same however often and from wherever it is read, so a
# repeated read, a restart and a replay never publish it twice. A reply read from before this sink began is offered
# again (a crash may have ended the process before it was delivered) once the agent has a publication under this
# key scheme; the first sink of an agent that has none is at the switch from the old keys, and delivers only what the
# record gains from its start (`_delivery_floor`). A turn's end is waited for in the record (`finish`), the turn's
# closing row, for at most
# `END_WAIT_SECONDS`, and a turn that did something with no record to show for it fails. A reply the sink could not
# deliver (a database error in `_deliver`) is not retried by the sink, the read position having moved on: the next
# invocation reads the record's last turn again and delivers it by key. A row of a kind the parser does not know is
# said once per kind by this sink (its own `UnreadRows`), so once per turn at most.
# meiva:link agent/graph/docs/harness-turn.md

DEBOUNCE_SECONDS = 0.5
END_WAIT_SECONDS = 2.0
END_POLL_SECONDS = 0.25


class ReadTimelineSink:
    def __init__(
        self,
        ctx: MeivaContext,
        agent_id: int,
        publisher: AgentEventPublisher,
        *,
        non_human_wake: bool,
        session_id: Callable[[], str | None],
    ) -> None:
        self._ctx = ctx
        self._agent_id = agent_id
        self._publisher = publisher
        self._non_human_wake = non_human_wake
        self._session_id = session_id
        self._harness = ctx.harness.value
        mode = ctx.require_agent().prompt.user_delivery_mode
        self._policy = ReplyPolicy(explicit_visibility=mode == "prefix", delivery_mode=mode)
        self._timing_revision: int | None = None
        self._timing_start: int | None = None
        self._replied = False
        self._warned = False
        self._projector = RecordProjector(
            policy=self._policy,
            render_tool=format_tool_use,
            wire_id=lambda key: wire_id(self._harness, key),
        )
        self._file: RecordFile | None = None
        self._from: int | None = None
        self._unread = UnreadRows()
        self._parser = entry_reader(self._harness, self._unread)
        self._last: ProjectedItem | None = None
        self._announced: set[int] = set()
        self._input_context: dict[str, InputRow] = {}
        self._dirty = asyncio.Event()
        self._busy = asyncio.Lock()
        self._active = False
        self._delivered_output = False
        self._closed_turns = 0
        self._invalid = False
        self._history_end = self._start_offset()
        self._floor: int | None = None
        self._input_keys: list[str] = []
        self._fork_source_loaded = False
        self._fork_source_id: int | None = None
        self._fork_floor: int | None = None
        self._fork_floor_loaded = False
        self._parent_uuids_loaded = False
        self._parent_uuids: frozenset[str] | None = None
        self._turn_timing = HarnessTurnTimingTracker(
            ctx,
            agent_id,
            publisher,
            session_id=session_id,
            initial_end=self._history_end,
        )

    def note_input_keys(self, keys: Sequence[str]) -> None:
        for key in keys:
            if key and key not in self._input_keys:
                self._input_keys.append(key)

    def on_turn_started(self, turn_id: str) -> None:
        raw_start = self._from if self._from is not None else self._start_offset()
        self._turn_timing.start_turn(
            turn_id,
            start_offset=max(raw_start, self._fork_floor or 0),
        )

    def clear_open_turn(self) -> None:
        self._turn_timing.clear_open_turn()

    @property
    def has_activity(self) -> bool:
        return self._active

    @property
    def non_human_wake(self) -> bool:
        return self._non_human_wake

    @property
    def has_delivered_output(self) -> bool:
        return self._delivered_output

    def reset_turn_delivery(self) -> None:
        self._delivered_output = False

    def take_invalid_reply(self) -> bool:
        invalid, self._invalid = self._invalid, False
        return invalid

    def mark_human(self) -> None:
        self._non_human_wake = False

    # meiva:note
    # The events are signals that the record grew, and the text of a finished reply is also checked here against the
    # visibility prefix the agent was told to write: the correction does not depend on the record being found.
    async def on_event(self, event: BridgeEvent) -> None:
        self._active = True
        if isinstance(event, (AssistantText, TextDelta, SubagentText, ToolUse, ToolResult)):
            self._delivered_output = True
        if isinstance(event, AssistantText):
            self._check_reply(event.text)
        self._dirty.set()

    def _check_reply(self, text: str) -> None:
        self._replied = True
        if (
            self._policy.explicit_visibility
            and text.strip()
            and parse_reply_visibility(text) is None
        ):
            self._invalid = True

    async def record_control(self, text: str, *, error: bool = False) -> None:
        logger.warning(
            "a harness control event the record does not show: {text}",
            event="harness_control_not_shown",
            agent_id=self._agent_id,
            text=text,
            error=error,
        )

    async def append_inbound[T](
        self,
        commit: Callable[[], Awaitable[T]],
        *,
        human_input: bool = False,
    ) -> T:
        result = await commit()
        if human_input:
            self.mark_human()
        self._dirty.set()
        return result

    async def flush_text(self) -> None:
        while True:
            _ = await self._dirty.wait()
            await asyncio.sleep(DEBOUNCE_SECONDS)
            self._dirty.clear()
            await self._read_logged()

    async def finish(
        self,
        turns: Sequence[HarnessTurn] = (),
        *,
        interrupted: bool = False,
        error: str | None = None,
        input_keys: Sequence[str] = (),
        turn_id: str | None = None,
        ended_at: datetime | None = None,
        fallback_duration_ms: int = 0,
    ) -> None:
        self.note_input_keys(input_keys)
        if not self._replied and not interrupted:
            for turn in turns:
                if turn.text:
                    self._check_reply(turn.text)
        self._replied = False
        clock = self._ctx.require_clock()
        settled_at = ended_at if ended_at is not None else clock.now()
        before, deadline = self._closed_turns, clock.monotonic() + END_WAIT_SECONDS
        wait = bool(turns) or interrupted or error is not None
        while True:
            await self._read_logged()
            if not wait or self._closed_turns > before or clock.monotonic() >= deadline:
                self._require_record(ended=wait, said=any(turn.text for turn in turns))
                end_offset = self._from if self._from is not None else self._start_offset()
                self._turn_timing.close_turn(
                    turn_id=turn_id,
                    end_offset=end_offset,
                    ended_at=settled_at,
                    fallback_duration_ms=fallback_duration_ms,
                )
                return
            await asyncio.sleep(END_POLL_SECONDS)

    # meiva:note
    # A turn that did something and ends with no record file to show for it is a failed turn, not a quiet one: nothing of
    # it can reach the timeline or the user, so it fails the way any harness setup failure does (the page's live
    # `Error` event, as for a built-in agent). The message names the session and where it was looked for.
    def _require_record(self, *, ended: bool, said: bool) -> None:
        if not (ended and (self._active or said)) or self._resolve() is not None:
            return
        ctx = self._ctx
        workspace = workspace_dir(self._agent_id, home=ctx.require_meiva_home())
        where = record_location(self._harness, ctx.require_environ(), workspace)
        sid = self._session_id()
        name = (
            None
            if sid is None
            else unreadable_file(
                self._harness, ctx.require_environ(), sid, packed_is_readable=False
            )
        )
        if name is not None:
            logger.warning(
                "the record of session {session_id} is in a form the live timeline does not read: {name}",
                event="harness_record_unreadable",
                agent_id=self._agent_id,
                session_id=sid,
                name=name,
            )
        raise HarnessSetupError(
            f"the record of the {ctx.harness.value} session {self._session_id() or '(not started)'} was not found "
            f"in {where} or any other project directory; nothing of this turn can be shown or delivered"
        )

    async def _read_logged(self) -> None:
        try:
            await self._read()
        except Exception:
            logger.opt(exception=True).warning(
                "could not read the harness's record for the live timeline; the next read tries again",
                event="harness_live_timeline_failed",
                agent_id=self._agent_id,
            )

    async def _read(self) -> None:
        async with self._busy:
            projected, end = await asyncio.to_thread(self._project_new)
            if projected is None:
                await self._refresh_timing(end)
                self._warn_unfound()
                return
            self._closed_turns += projected.turns_closed
            self._publish(projected, end)
            await self._deliver(projected.items)
            await self._refresh_timing(end)

    # meiva:note
    # Timing facts revise only reasoning items, with their existing row IDs. This projector gets
    # no text, inputs, tools or delivery calls, so a revision cannot republish a user's reply.
    # meiva:link base/lm/harness/antigravity/docs/antigravity.md
    async def _refresh_timing(self, end: int) -> None:
        file = self._file
        if file is None or self._timing_start is None:
            return
        revision = await asyncio.to_thread(file.revision)
        previous = self._timing_revision
        if previous is None:
            self._timing_revision = revision
            return
        if previous == revision:
            return
        updated = await asyncio.to_thread(
            reasoning_refresh,
            file,
            harness=self._harness,
            start=self._timing_start,
            policy=self._policy,
            render_tool=format_tool_use,
            unread=self._unread,
        )
        if updated.items:
            self._turn_timing.refresh(updated.items)
            self._publisher.emit(
                TimelineSnapshot(
                    agent_id=self._agent_id,
                    items=[item.item.model_dump() for item in updated.items],
                    msg_count=end,
                    active_turn_id=self._turn_timing.active_turn_id,
                    active_turn_started_at=self._turn_timing.active_turn_started_at,
                ).model_dump_json()
            )

        self._timing_revision = revision

    def _warn_unfound(self) -> None:
        if self._file is None and not self._warned:
            self._warned = True
            logger.warning(
                "the harness is running but its record file was not found; nothing of this turn reaches the "
                "timeline or the user until it is",
                event="harness_record_not_found",
                agent_id=self._agent_id,
            )

    # meiva:note
    # The new rows as a projection, and where they end; nothing when there is no file or nothing new.
    # Sample supplemental facts before reading rows: a commit during later delivery must still
    # differ from the revision that produced the first projection.
    def _project_new(self) -> tuple[Projected | None, int]:
        file = self._resolve()
        if file is None:
            return None, 0
        if not self._ensure_fork_floor(file):
            return None, file.complete_end()
        start = self._from if self._from is not None else self._turn_start(file)
        if self._fork_floor is not None and start < self._fork_floor:
            start = self._fork_floor
        if self._timing_start is None:
            self._timing_start = start
        if self._timing_revision is None:
            self._timing_revision = file.revision()
        block = file.rows_after(start)
        self._from = block.end
        if not block.rows:
            return None, block.end
        self._register_sent(block.rows)
        entries = self._parser.entries(block.rows)
        return self._projector.feed(
            entries, tail=self._parser.pending_calls, release_held=False
        ), block.end

    def _turn_start(self, file: RecordFile) -> int:
        window = read_window(
            file,
            harness=self._harness,
            limit=1,
            before=None,
            policy=ReplyPolicy(explicit_visibility=False, delivery_mode=None),
            render_tool=format_tool_use,
            load_sent=lambda _until, _wanted: {},
            unread=self._unread,
        )
        return window.turn_start if window.turn_start is not None else 0

    # meiva:note
    # The inbox is asked only when the new rows hold a person's input (a user row that is not a tool result, or an absorbed
    # message): a read of an assistant's rows and tool results opens no connection.
    def _register_sent(self, rows: Sequence[FileRow]) -> None:
        wanted = input_wire_ids(self._harness, rows)
        if not wanted:
            return
        if any(row.text is not None for row in wanted.values()):
            self._input_context.update(wanted)
            floor = max(row.stamp for row in wanted.values()) - (CLAIM_SLACK + CLAIM_EARLY)
            self._input_context = {
                key: row for key, row in self._input_context.items() if row.stamp >= floor
            }
            self._input_context = dict(list(self._input_context.items())[-1000:])
            wanted = self._input_context
        with self._ctx.require_db().connect() as conn:
            found = sent_inputs(
                load_sent_rows(
                    conn,
                    self._agent_id,
                    until=max(row.stamp for row in wanted.values()),
                    wanted=wanted,
                    wire_id=lambda key: wire_id(self._harness, key),
                ).rows
            )
        for key, items in found.items():
            if not self._projector.knows(key):
                fresh = [
                    item
                    for item in items
                    if item.inbound_id is None or item.inbound_id not in self._announced
                ]
                self._projector.register(key, fresh)

    def _resolve(self) -> RecordFile | None:
        if self._file is not None:
            return self._file
        sid = self._session_id()
        if sid is None:
            return None
        ctx = self._ctx
        path = session_file(
            self._harness,
            ctx.require_environ(),
            workspace_dir(self._agent_id, home=ctx.require_meiva_home()),
            sid,
        )
        if path is None:
            return None
        self._file = record_file(self._harness, path)
        return self._file

    # meiva:note
    # The end of the record as the sink began: what lies before it was written by an earlier process.
    def _start_offset(self) -> int:
        file = self._resolve()
        return 0 if file is None else file.complete_end()

    def _publish(self, projected: Projected, end: int) -> None:
        shown = {item.item.item_id for item in projected.items}
        running = [item for item in projected.pending if item.item.item_id not in shown]
        sent = [*projected.items, *running]
        if not sent:
            return
        self._turn_timing.apply(sent)
        carried = [] if self._last is None else [self._last]
        self._publisher.emit(
            TimelineSnapshot(
                agent_id=self._agent_id,
                items=[item.item.model_dump() for item in (*carried, *sent)],
                msg_count=end,
                active_turn_id=self._turn_timing.active_turn_id,
                active_turn_started_at=self._turn_timing.active_turn_started_at,
            ).model_dump_json()
        )
        self._last = max(sent, key=lambda item: item.offset)
        self._emit_committed_inbounds(sent)

    def _emit_committed_inbounds(self, sent: Sequence[ProjectedItem]) -> None:
        for item in sent:
            inbound = item.item.inbound_id
            if (
                item.item.kind == "inbound_chat"
                and inbound is not None
                and inbound not in self._announced
            ):
                self._announced.add(inbound)
                self._publisher.emit(
                    InboundCommitted(agent_id=self._agent_id, inbound_id=inbound).model_dump_json()
                )

    async def _deliver(self, items: Sequence[ProjectedItem]) -> None:
        floor = await self._delivery_floor()
        replies = [
            (reply_key(self._harness, item.key, self._session_id()), item.item)
            for item in items
            if item.item.kind == "agent_chat" and item.offset >= floor
        ]
        if replies:
            await capture_keyed_items(self._ctx, self._agent_id, replies, tolerate_conflicts=True)

    # meiva:note
    # Where delivery starts, decided once per sink. An agent that has no publication under the new key scheme yet is at
    # the switch from the old one: the replies before this sink's start were delivered under the old keys (or are not
    # worth delivering now), so only what the record gains from here is. An agent that has one is past the switch, and
    # everything is offered, which the key makes a no-op for what was delivered. A Claude Code fork never falls below
    # its persisted fork floor, because its record carries the parent's pre-fork rows and key-dedup cannot filter rows
    # this child never published.
    async def _delivery_floor(self) -> int:
        if self._floor is None:

            def switched() -> bool:
                with self._ctx.require_db().connect() as conn:
                    return has_keyed_publication(
                        conn, self._agent_id, reply_key_prefix(self._harness)
                    )

            base = 0 if await asyncio.to_thread(switched) else self._history_end
            self._floor = max(base, self._fork_floor or 0)
        return max(self._floor, self._fork_floor or 0)

    def _fork_source(self) -> int | None:
        if not self._fork_source_loaded:
            self._fork_source_loaded = True
            if self._harness == Harness.CLAUDE_CODE.value and self._ctx.db is not None:
                with self._ctx.db.connect() as conn:
                    row = conn.execute(
                        "SELECT fork_source_agent_id FROM agents_meta WHERE id = %s",
                        (self._agent_id,),
                    ).fetchone()
                self._fork_source_id = (
                    row[0] if row is not None and isinstance(row[0], int) else None
                )
        return self._fork_source_id

    # meiva:note
    # A Claude Code fork writes its parent's copied rows into its new `.jsonl` on first start and
    # stamps copied `user` rows with the child's first `promptId`, so `_turn_start` lands inside
    # the inherited prefix while `_history_end` was 0 at `__init__`. Anchor `self._fork_floor` once
    # at the byte offset where the child's own rows begin (using child input wire IDs, else the
    # parent record's UUID boundary) and persist it per `(agent_id, harness, session_id)` so
    # later turns and post-interrupt rewinds never publish parent replies.
    def _ensure_fork_floor(self, file: RecordFile) -> bool:
        if self._fork_floor_loaded:
            return True
        parent_id = self._fork_source()
        if parent_id is None:
            self._fork_floor = None
            self._fork_floor_loaded = True
            return True
        sid = self._session_id()
        if sid is None:
            return False
        home = self._ctx.require_meiva_home()
        floor_path = _fork_floor_path(home, self._agent_id, self._harness)
        stored = _read_fork_floor(floor_path, session_id=sid)
        if stored is not None:
            self._fork_floor = stored
            self._fork_floor_loaded = True
            return True
        cut = self._resolve_fork_cut(file, parent_id=parent_id, home=home)
        if cut is None:
            return False
        _write_fork_floor(floor_path, session_id=sid, floor=cut)
        self._fork_floor = cut
        self._fork_floor_loaded = True
        return True

    def _resolve_fork_cut(self, file: RecordFile, *, parent_id: int, home: Path) -> int | None:
        keys = self._input_keys or self._child_db_input_keys()
        if keys:
            primary = wire_id(self._harness, self._input_keys[0]) if self._input_keys else None
            target_uuids = {wire_id(self._harness, key) for key in keys}
            matched = _find_earliest_input_offset(file, target_uuids, primary)
            if matched is not None:
                return matched
        parent_uuids = self._parent_record_uuids(parent_id, home=home)
        if parent_uuids:
            parent_end, saw_child = _parent_fork_cut(file, parent_uuids)
            if parent_end is not None:
                return parent_end if saw_child else None
        if self._history_end > 0:
            return self._history_end
        return None

    def _child_db_input_keys(self) -> list[str]:
        if self._ctx.db is None:
            return []
        with self._ctx.db.connect() as conn:
            rows = conn.execute(
                "SELECT id, kind, payload FROM inbound_messages "
                "WHERE agent_id = %s "
                "AND (kind = %s OR (kind = ANY(%s) AND claimed_at IS NOT NULL)) "
                "ORDER BY created_at ASC, id ASC LIMIT 20",
                (self._agent_id, InboundKind.FORK.value, list(DELIVERABLE_KINDS)),
            ).fetchall()
        keys: list[str] = []
        for row_id, kind, payload in rows:
            if kind == InboundKind.FORK.value:
                keys.append(f"control-{row_id}")
            else:
                keys.append(
                    recorded_input_key(row_id, payload if is_object_dict(payload) else None)
                )
        return keys

    def _parent_record_uuids(self, parent_id: int, *, home: Path) -> frozenset[str] | None:
        if self._parent_uuids_loaded:
            return self._parent_uuids
        self._parent_uuids_loaded = True
        registry = FileSessionRegistry(harness_sessions_dir(home=home))
        origin = registry.load(parent_id, harness=self._harness)
        if origin is None:
            return None
        parent_path = session_file(
            self._harness, self._ctx.require_environ(), Path(origin.cwd), origin.session_id
        )
        if parent_path is None:
            return None
        self._parent_uuids = frozenset(_read_row_uuids(parent_path))
        return self._parent_uuids


def _row_uuid(row: Mapping[str, object]) -> str | None:
    attachment = row.get("attachment")
    if is_object_dict(attachment):
        source = attachment.get("source_uuid")
        if isinstance(source, str) and source:
            return source
    value = row.get("uuid")
    return value if isinstance(value, str) and value else None


def _is_child_conversation_row(row: Mapping[str, object]) -> bool:
    return row.get("type") in ("user", "assistant")


def _find_earliest_input_offset(
    file: RecordFile, target_uuids: set[str], primary_uuid: str | None
) -> int | None:
    earliest: int | None = None
    pos = file.complete_end()
    while pos > 0:
        block = file.rows_before(pos, BLOCK_BYTES)
        found_primary = False
        for row in block.rows:
            uid = _row_uuid(row.row)
            if uid in target_uuids:
                if earliest is None or row.offset < earliest:
                    earliest = row.offset
                if primary_uuid is not None and uid == primary_uuid:
                    found_primary = True
        if found_primary or block.start == 0 or block.start >= pos:
            break
        pos = block.start
    return earliest


def _read_row_uuids(path: Path) -> set[str]:
    uuids: set[str] = set()
    try:
        with path.open("rb") as handle:
            for raw_line in handle:
                try:
                    parsed = object_of(json.loads, raw_line.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if is_object_dict(parsed) and (uid := _row_uuid(parsed)) is not None:
                    uuids.add(uid)
    except OSError:
        return set()
    return uuids


def _parent_fork_cut(file: RecordFile, parent_uuids: frozenset[str]) -> tuple[int | None, bool]:
    saw_child = False
    pos = file.complete_end()
    while pos > 0:
        block = file.rows_before(pos, BLOCK_BYTES)
        for row in reversed(block.rows):
            uid = _row_uuid(row.row)
            if uid is not None and uid in parent_uuids:
                return row.end, saw_child
            if _is_child_conversation_row(row.row):
                saw_child = True
        if block.start == 0 or block.start >= pos:
            break
        pos = block.start
    return None, saw_child


def _fork_floor_path(home: Path, agent_id: int, harness: str) -> Path:
    return harness_sessions_dir(home=home) / f"{agent_id}.{harness}.floor"


def _read_fork_floor(path: Path, *, session_id: str) -> int | None:
    try:
        raw = object_of(json.loads, path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not is_object_dict(raw) or raw.get("session_id") != session_id:
        return None
    floor = raw.get("floor")
    if isinstance(floor, int) and not isinstance(floor, bool) and floor >= 0:
        return floor
    return None


def _write_fork_floor(path: Path, *, session_id: str, floor: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".floor-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps({"session_id": session_id, "floor": floor}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


# meiva:note
# The live session's id once the CLI has reported it, before that the one the session registry holds for the agent,
# which is the one the CLI is resuming.
def session_of(ctx: MeivaContext, agent_id: int, harness: str) -> Callable[[], str | None]:
    registry = FileSessionRegistry(harness_sessions_dir(home=ctx.require_meiva_home()))

    def current() -> str | None:
        live = getattr(ctx.harness_bridge, "session_id", None)
        if isinstance(live, str):
            return live
        record = registry.load(agent_id, harness=harness)
        return None if record is None else record.session_id

    return current


__all__ = ["ReadTimelineSink", "session_of"]
