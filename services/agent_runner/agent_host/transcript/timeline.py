from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.tool_format import format_tool_use
from agent.harness_timeline.writer import HarnessTimelineWriter
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.host.env.agent_slices import AgentSlices
from base.lm.harness.registry import SessionRegistry
from base.lm.harness.session.transcript.ports import VersionedRecordReader
from base.lm.harness.wiring import transcript_reader_for
from base.log import logger
from base.native_process.runtime_incarnation import RuntimeIncarnation
from base.paths import workspace_dir
from services.agent_runner.agent_host.transcript.effects import TimelineEffects

# meiva:note
# The runtime-scoped owner of one external Claude agent's transcript-sourced timeline: it builds the session-scoped
# writer (`agent/harness_timeline`) and its effects, runs the writer as one task for as long as the runtime lives,
# and is the only thing the host has to bind, wake and close. It is built with the runtime (when the agent's
# `harness_transcript_timeline` setting is on) and closed with it, on every path that closes the bridge: idle
# eviction, `drop_agent`, a failed turn's drop and host shutdown, each with a last read before and after the CLI
# goes away.
#
# What flows where. The CLI's own transcript is the only source of timeline content. The Claude SDK's
# `session_store` (`WakeSessionStore`, given to the session through `session_wake`) calls `wake` when the CLI's
# mirror appends, which only wakes the writer; a low-frequency poll covers a mirror batch the SDK dropped. The
# graph invocation holds the writer through `MeivaContext.harness_timeline` (it registers what it sends and waits
# for the turn's closing entry) and the host binds, at every hosted turn, that turn's context (which the effects
# publish, capture and price with) and its admitted runtime incarnation (`bind_turn`): the writer outlives the
# invocations, and between turns it keeps the last bound ones, which stay valid while the same host owns the
# agent's row.
# meiva:link agent/harness_timeline/README.md


class TimelinePublisher(Protocol):
    def emit(self, payload: str) -> None: ...

    async def start(self, task_group: asyncio.TaskGroup) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class TimelineDeps:
    pool: AsyncConnectionPool
    clock: Clock
    marker_id: Callable[[], str]
    publisher_factory: Callable[[int], TimelinePublisher]


_CLOSE_JOIN_SECONDS = 10.0


# meiva:note
# The session the writer reads: the live session's id once the CLI has reported it, and before that the one the
# session registry holds for the agent, which is the one the CLI is resuming (the id is known to the live session
# only after its init event, which comes after the invocation has already had to decide which of the inputs an
# earlier process left claimed the record shows). A new agent has neither, and then there is no record to read.
# meiva:link agent/harness_timeline/README.md
def session_with_resume(
    live: Callable[[], str | None], registry: SessionRegistry, *, agent_id: int, harness: str
) -> Callable[[], str | None]:
    def current() -> str | None:
        session_id = live()
        if session_id is not None:
            return session_id
        record = registry.load(agent_id, harness=harness)
        return None if record is None else record.session_id

    return current


class TranscriptTimeline:
    def __init__(
        self,
        deps: TimelineDeps,
        *,
        agent_id: int,
        harness: str,
        policy: ReplyPolicy,
        reader_for: Callable[[Callable[[], str | None]], VersionedRecordReader],
    ) -> None:
        self._agent_id = agent_id
        self._session_id: Callable[[], str | None] = lambda: None
        self._publisher = deps.publisher_factory(agent_id)
        self.effects = TimelineEffects(agent_id, self._publisher)
        reader = reader_for(self._current_session)
        self.writer = HarnessTimelineWriter(
            pool=deps.pool,
            agent_id=agent_id,
            harness=harness,
            session_id=self._current_session,
            wire_id=reader.wire_id,
            marker_id=deps.marker_id,
            incarnation=None,
            reader=reader,
            policy=policy,
            render_tool=format_tool_use,
            after_commit=self.effects.after_commit,
            cli_version=lambda: reader.cli_version,
            clock=deps.clock,
            release_held_at_turn_end=True,
        )
        self._running = False
        self._stopped = asyncio.Event()
        self._stopped.set()
        self._closed = False

    def attach(self, session_id: Callable[[], str | None]) -> None:
        self._session_id = session_id

    def _current_session(self) -> str | None:
        return self._session_id()

    def wake(self, subpath: str | None) -> None:
        del subpath
        self.writer.wake()

    def bind_turn(self, ctx: MeivaContext, incarnation: RuntimeIncarnation) -> None:
        self.effects.bind(ctx)
        self.writer.bind_incarnation(incarnation)

    @property
    def wants_task(self) -> bool:
        return not self._running and not self._closed

    # meiva:note
    # The writer's loop as a task the host's session owner runs in its own task group (`HarnessSessions.run_timeline`),
    # one at a time per runtime: the host starts it at the first hosted turn and again at a later one if the writer
    # stopped on its own fault (a started writer clears the fault). What the loop keeps lives in the database (the
    # positions and the stored rows), so a stopped writer loses nothing. It logs the failure it ended with instead of
    # raising, so one agent's writer can never cancel the other agents' tasks in the group.
    async def run(self) -> BaseException | None:
        self._running = True
        self._stopped.clear()
        self.writer.clear_fault()
        try:
            async with asyncio.TaskGroup() as group:
                await self._publisher.start(group)
                try:
                    await self.effects.recover(self.writer.recent_replies)
                    await self.writer.run()
                except Exception:
                    logger.opt(exception=True).error(
                        "the transcript timeline writer stopped with an unexpected error",
                        event="harness_timeline_writer_faulted",
                        agent_id=self._agent_id,
                    )
                finally:
                    await self._publisher.aclose()
        finally:
            self._running = False
            self._stopped.set()
        return None

    # meiva:note
    # A last read, then the end of the writer. Reading never fails the close: what the CLI wrote and this could
    # not read stays in the CLI's file, and the next writer starts from its stored position.
    async def read_final(self) -> None:
        try:
            while await self.writer.read_now(exhaustive=True):
                pass
        except Exception:
            logger.opt(exception=True).warning(
                "the final read of the transcript before closing failed",
                event="harness_timeline_writer_retry",
                agent_id=self._agent_id,
            )

    async def _release_held(self) -> None:
        try:
            await self.writer.release_held()
        except Exception:
            logger.opt(exception=True).warning(
                "the writer could not show what it held back before closing",
                event="harness_timeline_writer_retry",
                agent_id=self._agent_id,
            )

    async def close(self) -> None:
        self._closed = True
        await self.read_final()
        await self._release_held()
        self.writer.stop()
        try:
            await asyncio.wait_for(self._stopped.wait(), _CLOSE_JOIN_SECONDS)
        except TimeoutError:
            logger.warning(
                "the transcript timeline writer did not stop in time; it is retired and reads and writes nothing more",
                event="harness_timeline_writer_retired",
                agent_id=self._agent_id,
            )
            await self.writer.retire()


# meiva:note
# The transcript-sourced timeline of this runtime, or None: only an external Claude Code agent whose
# `harness_transcript_timeline` setting is on gets one, and only a host that gave the runtime its dependencies
# can build it. The setting on the built-in agent means nothing and is not reported; on another external harness it
# changes nothing (the other CLIs cut over later) and says so, once per runtime built.
# meiva:link agent/harness_timeline/README.md
def transcript_timeline_for(
    agent_id: int,
    harness: Harness,
    slices: AgentSlices,
    deps: TimelineDeps | None,
    *,
    home: Path,
    harness_environ: Mapping[str, str],
) -> TranscriptTimeline | None:
    if not slices.harness_cli.harness_transcript_timeline or deps is None:
        return None
    if harness is Harness.MEIVA:
        return None
    if harness is not Harness.CLAUDE_CODE:
        logger.warning(
            "agent {agent_id}: harness_transcript_timeline is on but only Claude Code agents read their "
            "timeline from the transcript; the setting is ignored for {harness}",
            event="harness_timeline_unsupported_harness",
            agent_id=agent_id,
            harness=harness.value,
        )
        return None
    mode = slices.prompt.user_delivery_mode
    return TranscriptTimeline(
        deps,
        agent_id=agent_id,
        harness=harness.value,
        policy=ReplyPolicy(explicit_visibility=mode == "prefix", delivery_mode=mode),
        reader_for=lambda session_id: transcript_reader_for(
            harness,
            environ=harness_environ,
            workspace=workspace_dir(agent_id, home=home),
            session_id=session_id,
            now_ns=lambda: round(deps.clock.now().timestamp() * 1_000_000_000),
        ),
    )
