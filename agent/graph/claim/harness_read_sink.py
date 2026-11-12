from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence

from agent.graph.hooks.user_delivery import capture_keyed_items
from base.agents.context import MeivaContext
from base.agents.history.timeline_item import TimelineItem
from base.agents.history.transcript_timeline.items import ReplyPolicy, format_tool_use
from base.agents.history.transcript_timeline.project import (
    Projected,
    ProjectedItem,
    RecordProjector,
)
from base.agents.history.transcript_timeline.record.harness import (
    entry_reader,
    input_wire_ids,
    record_location,
    reply_key,
    reply_key_prefix,
    session_file,
    unreadable_file,
    wire_id,
)
from base.agents.history.transcript_timeline.record.read import read_window
from base.agents.history.transcript_timeline.sent import load_sent_rows, sent_inputs
from base.agents.messages.reply_intent import parse_reply_visibility
from base.agents.to_user.delivery.store import has_keyed_publication
from base.events.live.projection import InboundCommitted, TimelineSnapshot
from base.events.live.publisher import AgentEventPublisher
from base.lm.harness import AssistantText, BridgeEvent, HarnessSetupError
from base.lm.harness.engine import HarnessTurn
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.session.transcript.record_file import FileRow, RecordFile
from base.log import logger
from base.paths import harness_sessions_dir, workspace_dir

# meiva:note
# The timeline of a Claude Code or Codex agent's turn while it runs. The CLI's events say only that something new happened; what
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
# invocation reads the record's last turn again and delivers it by key.
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
        self._replied = False
        self._warned = False
        self._projector = RecordProjector(
            policy=self._policy,
            render_tool=format_tool_use,
            wire_id=lambda key: wire_id(self._harness, key),
        )
        self._file: RecordFile | None = None
        self._from: int | None = None
        self._parser = entry_reader(self._harness)
        self._last: ProjectedItem | None = None
        self._announced: set[int] = set()
        self._dirty = asyncio.Event()
        self._busy = asyncio.Lock()
        self._active = False
        self._closed_turns = 0
        self._invalid = False
        self._history_end = self._start_offset()
        self._floor: int | None = None

    @property
    def has_activity(self) -> bool:
        return self._active

    @property
    def non_human_wake(self) -> bool:
        return self._non_human_wake

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

    async def append_inbound(
        self,
        commit: Callable[[], Awaitable[list[TimelineItem]]],
        *,
        human_input: bool = False,
    ) -> list[TimelineItem]:
        items = await commit()
        if human_input:
            self.mark_human()
        self._dirty.set()
        return items

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
    ) -> None:
        del input_keys
        if not self._replied and not interrupted:
            for turn in turns:
                if turn.text:
                    self._check_reply(turn.text)
        self._replied = False
        clock = self._ctx.require_clock()
        before, deadline = self._closed_turns, clock.monotonic() + END_WAIT_SECONDS
        wait = bool(turns) or interrupted or error is not None
        while True:
            await self._read_logged()
            self._require_record(ended=wait, said=any(turn.text for turn in turns))
            if not wait or self._closed_turns > before or clock.monotonic() >= deadline:
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
        name = None if sid is None else unreadable_file(self._harness, ctx.require_environ(), sid)
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
                self._warn_unfound()
                return
            self._closed_turns += projected.turns_closed
            self._publish(projected, end)
            await self._deliver(projected.items)

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
    def _project_new(self) -> tuple[Projected | None, int]:
        file = self._resolve()
        if file is None:
            return None, 0
        start = self._from if self._from is not None else self._turn_start(file)
        block = file.rows_after(start)
        self._from = block.end
        if not block.rows:
            return None, block.end
        self._register_sent(block.rows)
        entries = self._parser.entries(block.rows)
        return self._projector.feed(entries, tail=True, release_held=False), block.end

    def _turn_start(self, file: RecordFile) -> int:
        window = read_window(
            file,
            harness=self._harness,
            limit=1,
            before=None,
            policy=ReplyPolicy(explicit_visibility=False, delivery_mode=None),
            render_tool=format_tool_use,
            load_sent=lambda _until, _wanted: {},
        )
        return window.turn_start if window.turn_start is not None else 0

    # meiva:note
    # The inbox is asked only when the new rows hold a person's input (a user row that is not a tool result, or an absorbed
    # message): a read of an assistant's rows and tool results opens no connection.
    def _register_sent(self, rows: Sequence[FileRow]) -> None:
        wanted = input_wire_ids(self._harness, rows)
        if not wanted:
            return
        with self._ctx.require_db().connect() as conn:
            found = sent_inputs(
                load_sent_rows(
                    conn,
                    self._agent_id,
                    until=max(wanted.values()),
                    wanted=wanted,
                    wire_id=lambda key: wire_id(self._harness, key),
                ).rows
            )
        for key, items in found.items():
            if not self._projector.knows(key):
                self._projector.register(key, items)

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
        self._file = RecordFile(path)
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
        carried = [] if self._last is None else [self._last]
        self._publisher.emit(
            TimelineSnapshot(
                agent_id=self._agent_id,
                items=[item.item.model_dump() for item in (*carried, *sent)],
                msg_count=end,
            ).model_dump_json()
        )
        self._last = max(sent, key=lambda item: item.offset)
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
            (reply_key(self._harness, item.key), item.item)
            for item in items
            if item.item.kind == "agent_chat" and item.offset >= floor
        ]
        if replies:
            await capture_keyed_items(self._ctx, self._agent_id, replies, tolerate_conflicts=True)

    # meiva:note
    # Where delivery starts, decided once per sink. An agent that has no publication under the new key scheme yet is at
    # the switch from the old one: the replies before this sink's start were delivered under the old keys (or are not
    # worth delivering now), so only what the record gains from here is. An agent that has one is past the switch, and
    # everything is offered, which the key makes a no-op for what was delivered.
    async def _delivery_floor(self) -> int:
        if self._floor is None:

            def switched() -> bool:
                with self._ctx.require_db().connect() as conn:
                    return has_keyed_publication(
                        conn, self._agent_id, reply_key_prefix(self._harness)
                    )

            self._floor = 0 if await asyncio.to_thread(switched) else self._history_end
        return self._floor


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
