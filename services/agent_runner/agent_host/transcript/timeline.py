from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.tool_format import format_tool_use
from agent.harness_timeline.writer import HarnessTimelineWriter
from base.agents.context import MeivaContext
from base.api_contracts.agent_contract import Harness
from base.clock import Clock
from base.host.env.agent_slices import AgentSlices
from base.lm.harness.session.transcript.ports import RecordReader
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


@dataclass(frozen=True)
class TimelineDeps:
    pool: AsyncConnectionPool
    clock: Clock
    marker_id: Callable[[], str]


_CLOSE_JOIN_SECONDS = 10.0


class TranscriptTimeline:
    def __init__(
        self,
        deps: TimelineDeps,
        *,
        agent_id: int,
        harness: str,
        policy: ReplyPolicy,
        reader_for: Callable[[Callable[[], str | None]], RecordReader],
    ) -> None:
        self._agent_id = agent_id
        self._session_id: Callable[[], str | None] = lambda: None
        self.effects = TimelineEffects(agent_id)
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
            cli_version=lambda: None,
            clock=deps.clock,
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
            await self.writer.run()
        except Exception:
            logger.opt(exception=True).error(
                "the transcript timeline writer stopped with an unexpected error",
                event="harness_timeline_writer_faulted",
                agent_id=self._agent_id,
            )
        finally:
            self._running = False
            self._stopped.set()
        return None

    # meiva:note
    # A last read, then the end of the writer. Reading never fails the close: what the CLI wrote and this could
    # not read stays in the CLI's file, and the next writer starts from its stored position.
    async def read_final(self) -> None:
        try:
            while await self.writer.read_now():
                pass
        except Exception:
            logger.opt(exception=True).warning(
                "the final read of the transcript before closing failed",
                event="harness_timeline_writer_retry",
                agent_id=self._agent_id,
            )

    async def close(self) -> None:
        self._closed = True
        await self.read_final()
        self.writer.stop()
        with suppress(TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), _CLOSE_JOIN_SECONDS)


# meiva:note
# The transcript-sourced timeline of this runtime, or None: only an external Claude Code agent whose
# `harness_transcript_timeline` setting is on gets one, and only a host that gave the runtime its dependencies
# can build it. The setting on another harness changes nothing (the other CLIs cut over later), and says so.
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
        ),
    )
