# meiva:note
# How the agent host wires the transcript-sourced timeline of an external Claude agent (the agent's
# `harness_transcript_timeline` setting, off by default): which runtimes get a writer, how the SDK's
# `session_store` reaches it as a wake-up only, that `load` never returns rows (so resume stays on the CLI's own
# file), and that every way of closing a runtime reads once more before the CLI goes away and stops the writer
# after it.
# meiva:link agent/harness_timeline/README.md

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import cast
from unittest.mock import Mock
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from agent.harness_timeline.plan import ReplyPolicy
from agent.harness_timeline.writer import HarnessTimelineWriter
from base.api_contracts.agent_contract import Harness
from base.clock.tests.fakes import system_clock
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.lm.harness import FileSessionRegistry, RegistrationStore, SessionRecord
from base.lm.harness.claude.session import ClaudeSession
from base.lm.harness.claude.transcript.wake import WakeSessionStore
from base.lm.harness.engine import TurnEngine
from base.lm.harness.session.transcript.entries import AnyEntry
from base.lm.harness.session.transcript.ports import EntryBatch, StoredRow
from base.lm.harness.tests.roots import bridge_root_values
from base.lm.harness.wiring import open_session
from base.log.tests.log_records import LogRecords
from base.native_process.runtime_incarnation import RuntimeIncarnation
from services.agent_runner.agent_host.runtime import AgentRuntime, HarnessSessions, build_runtime
from services.agent_runner.agent_host.transcript.timeline import (
    TimelineDeps,
    TranscriptTimeline,
    session_with_resume,
    transcript_timeline_for,
)


class QuietPublisher:
    def emit(self, payload: str) -> None:
        del payload

    async def start(self, task_group: asyncio.TaskGroup) -> None:
        del task_group

    async def aclose(self) -> None:
        return None


async def _build(
    tmp_path: Path,
    model_catalog: ModelCatalog,
    pool: AsyncConnectionPool,
    *,
    harness: Harness,
    on: bool,
) -> AgentRuntime:
    pins: dict[str, object] = {"harness_transcript_timeline": on}
    return await build_runtime(
        71,
        "timeline",
        AgentSlices.resolve(pins, default_reader=defaults()),
        catalog=model_catalog,
        keys=ProviderKeys({}),
        llm_override="",
        home=tmp_path,
        harness=harness,
        registry=FileSessionRegistry(tmp_path / "sessions"),
        registrations=None,
        registration_ttl_seconds=60,
        machine="external-test",
        harness_environ={"HOME": str(tmp_path)},
        harness_root=bridge_root_values(),
        timeline_deps=TimelineDeps(
            pool=pool,
            clock=system_clock(),
            marker_id=lambda: "m",
            publisher_factory=lambda _: QuietPublisher(),
        ),
    )


async def test_only_a_claude_agent_with_the_setting_on_gets_a_writer(
    tmp_path: Path,
    model_catalog: ModelCatalog,
    aops_pool: AsyncConnectionPool,
    loguru_records: LogRecords,
) -> None:
    default = await _build(
        tmp_path, model_catalog, aops_pool, harness=Harness.CLAUDE_CODE, on=False
    )
    assert default.harness_timeline is None, "the setting is off by default"
    enabled = await _build(tmp_path, model_catalog, aops_pool, harness=Harness.CLAUDE_CODE, on=True)
    assert isinstance(enabled.harness_timeline, TranscriptTimeline)
    assert isinstance(enabled.harness_timeline.writer, HarnessTimelineWriter)
    other = await _build(tmp_path, model_catalog, aops_pool, harness=Harness.CODEX, on=True)
    assert other.harness_timeline is None
    assert len(loguru_records.events("harness_timeline_unsupported_harness")) == 1


async def test_the_session_store_only_wakes_the_writer_and_never_returns_rows(
    tmp_path: Path,
) -> None:
    woken: list[str | None] = []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    environ = {"PATH": str(bin_dir)}
    session = open_session(
        Harness.CLAUDE_CODE,
        agent_id=71,
        cwd=tmp_path,
        registry=FileSessionRegistry(tmp_path / "sessions"),
        environ=environ,
        clock=bridge_root_values().clock,
        session_wake=woken.append,
    )
    assert isinstance(session, ClaudeSession)
    store = session.config.session_store
    assert isinstance(store, WakeSessionStore)
    await store.append({"project_key": "p", "session_id": "s"}, [{"type": "user"}])
    await store.append({"project_key": "p", "session_id": "s", "subpath": "subagents/agent-a"}, [])
    assert woken == [None, "subagents/agent-a"]
    assert await store.load({"project_key": "p", "session_id": "s"}) is None
    plain = open_session(
        Harness.CLAUDE_CODE,
        agent_id=71,
        cwd=tmp_path,
        registry=FileSessionRegistry(tmp_path / "sessions"),
        environ=environ,
        clock=bridge_root_values().clock,
    )
    assert isinstance(plain, ClaudeSession) and plain.config.session_store is None


class _RecordingEngine:
    exit_code: int | None = 0

    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def close(self, *, reason: str = "closed") -> None:
        del reason
        self._calls.append("cli_close")


class _ExitRecorder:
    async def __call__(self, agent_id: int, exit_code: int | None) -> None:
        del agent_id, exit_code


class _Timeline:
    def __init__(self, calls: list[str]) -> None:
        self._calls = calls

    async def read_final(self) -> None:
        self._calls.append("read_final")

    async def close(self) -> None:
        self._calls.append("close")


async def test_a_retired_runtime_reads_once_more_before_the_cli_closes_and_stops_the_writer_after(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    engine = _RecordingEngine(calls)
    owner = HarnessSessions(
        RegistrationStore(tmp_path), record_exit=_ExitRecorder(), clock=system_clock()
    )
    await owner.start()
    runtime = AgentRuntime(
        fingerprint="old",
        llm=Mock(),
        harness=Harness.CODEX,
        harness_bridge=cast("TurnEngine", engine),
        harness_timeline=cast("TranscriptTimeline", _Timeline(calls)),
    )
    try:
        owner.retire(1, runtime)
        await owner.join(1)
    finally:
        await owner.aclose()
    assert calls == ["read_final", "cli_close", "close"]
    assert asyncio.get_running_loop() is not None


class _NoRecord:
    @property
    def cli_version(self) -> str | None:
        return None

    async def records(self, *, exhaustive: bool = False) -> tuple[str, ...]:
        return ()

    def wire_id(self, key: str) -> str:
        return key

    def parse_stored(self, rows: Sequence[StoredRow]) -> tuple[AnyEntry, ...]:
        del rows
        return ()

    async def position_before(
        self, record: str, wire_ids: Sequence[str], *, after_ordinal: int = -1
    ) -> str | None:
        del record, wire_ids
        return None

    async def read_after(self, record: str, position: str | None) -> EntryBatch | None:
        del record, position
        return None


async def test_the_host_binds_every_turn_and_runs_one_writer_task_until_the_runtime_closes(
    aops_pool: AsyncConnectionPool, tmp_path: Path
) -> None:
    timeline = TranscriptTimeline(
        TimelineDeps(
            pool=aops_pool,
            clock=system_clock(),
            marker_id=lambda: "m",
            publisher_factory=lambda _: QuietPublisher(),
        ),
        agent_id=71,
        harness="claude_code",
        policy=ReplyPolicy(explicit_visibility=False, delivery_mode=None),
        reader_for=lambda _: _NoRecord(),
    )
    ids: list[str | None] = [None]
    timeline.attach(lambda: ids[0])
    owner = HarnessSessions(
        RegistrationStore(tmp_path), record_exit=_ExitRecorder(), clock=system_clock()
    )
    await owner.start()
    first = RuntimeIncarnation(71, uuid4(), uuid4())
    try:
        timeline.bind_turn(Mock(), first)
        assert timeline.wants_task
        owner.run_timeline(71, timeline)
        await asyncio.sleep(0.05)
        assert not timeline.wants_task, "one writer task per runtime"
        second = RuntimeIncarnation(71, first.generation, first.owner)
        timeline.bind_turn(Mock(), second)
        owner.run_timeline(71, timeline)
        ids[0] = "sid"
        timeline.wake(None)
        await asyncio.wait_for(timeline.close(), 15)
        assert not timeline.wants_task, "a closed timeline starts nothing"
        owner.run_timeline(71, timeline)
    finally:
        await owner.aclose()
    assert owner.failures == []


def test_the_writer_reads_the_registered_session_until_the_live_one_reports_its_id(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    live: list[str | None] = [None]
    current = session_with_resume(
        lambda: live[0], registry, agent_id=71, harness=Harness.CLAUDE_CODE.value
    )
    assert current() is None, "a new agent has neither"
    registry.save(
        SessionRecord(
            agent_id=71,
            session_id="resumed-session",
            cwd=str(tmp_path),
            model="m",
            created_at="2026-11-12T00:00:00+00:00",
            updated_at="2026-11-12T00:00:00+00:00",
            harness=Harness.CLAUDE_CODE.value,
            generation=1,
        )
    )
    assert current() == "resumed-session", "an agent that is resuming reads its record before init"
    live[0] = "live-session"
    assert current() == "live-session"


def test_the_setting_on_the_built_in_agent_means_nothing_and_is_not_reported(
    tmp_path: Path, aops_pool: AsyncConnectionPool, loguru_records: LogRecords
) -> None:
    slices = AgentSlices.resolve({"harness_transcript_timeline": True}, default_reader=defaults())
    deps = TimelineDeps(
        pool=aops_pool,
        clock=system_clock(),
        marker_id=lambda: "m",
        publisher_factory=lambda _: QuietPublisher(),
    )
    built = transcript_timeline_for(
        71, Harness.MEIVA, slices, deps, home=tmp_path, harness_environ={"HOME": str(tmp_path)}
    )
    assert built is None
    assert loguru_records.events("harness_timeline_unsupported_harness") == []
