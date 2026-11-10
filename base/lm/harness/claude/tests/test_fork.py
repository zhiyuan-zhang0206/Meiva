from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

import pytest

from base.lm.harness import FileSessionRegistry, Handoff
from base.lm.harness.base_session import ForkSourceUnavailableError
from base.lm.harness.claude.options import fork_session_id
from base.lm.harness.claude.tests.fake_sdk import FakeSDK, Say
from base.lm.harness.claude.tests.support import Harness, start
from base.lm.harness.events import HandoffInjected, SessionBound
from base.lm.harness.registry import SessionRecord

SOURCE = 7
CHILD = 8


async def _source(tmp_path: Path, registry: FileSessionRegistry, sdk: FakeSDK) -> Harness:
    source = await start(
        tmp_path, agent_id=SOURCE, registry=registry, session_id="sess-src", sdk=sdk
    )
    await source.wait_for(lambda event: isinstance(event, SessionBound))
    await source.stop()
    return source


async def test_first_start_forks_the_sources_session_under_a_fixed_id(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    sdk = FakeSDK()
    await _source(tmp_path, registry, sdk)

    child = await start(
        tmp_path, agent_id=CHILD, registry=registry, sdk=sdk, fork_source_agent_id=SOURCE
    )
    try:
        options = sdk.clients[-1].options
        assert options.resume == "sess-src"
        assert options.fork_session is True
        assert options.session_id == fork_session_id(CHILD)
        bound = await child.wait_for(lambda event: isinstance(event, SessionBound))
        assert bound.session_id == fork_session_id(CHILD)
        assert bound.resumed is True
        record = registry.load(CHILD, harness="claude_code")
        assert record is not None
        assert record.session_id == fork_session_id(CHILD)
        source = registry.load(SOURCE, harness="claude_code")
        assert source is not None and source.session_id == "sess-src"
    finally:
        await child.stop()


async def test_a_fork_starts_with_the_sources_options_plus_the_fork_keys(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    sdk = FakeSDK()
    await _source(tmp_path, registry, sdk)
    child = await start(
        tmp_path, agent_id=CHILD, registry=registry, sdk=sdk, fork_source_agent_id=SOURCE
    )
    try:
        source_options = asdict(sdk.clients[0].options)
        fork_options = asdict(sdk.clients[-1].options)
        differing = {name for name in source_options if source_options[name] != fork_options[name]}
        assert differing == {"resume", "fork_session", "session_id"}
    finally:
        await child.stop()


async def test_a_fork_does_not_resend_the_handoff(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    sdk = FakeSDK()
    await _source(tmp_path, registry, sdk)
    handoff = Handoff(agent_id=CHILD, instruction="Say OK.", machine="m", workspace=tmp_path)
    child = await start(
        tmp_path,
        agent_id=CHILD,
        registry=registry,
        sdk=sdk,
        fork_source_agent_id=SOURCE,
        handoff=handoff,
        scripts=[[Say("ok")]],
    )
    try:
        assert child.session.resumed is True
        assert child.of(HandoffInjected) == []
        outcome = await child.session.submit("fork note").outcome()
        assert outcome.text == "ok"
        assert sdk.clients[-1].queries == ["fork note"]
    finally:
        await child.stop()


async def test_a_restart_resumes_the_clone_it_registered(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    sdk = FakeSDK()
    await _source(tmp_path, registry, sdk)
    first = await start(
        tmp_path, agent_id=CHILD, registry=registry, sdk=sdk, fork_source_agent_id=SOURCE
    )
    await first.wait_for(lambda event: isinstance(event, SessionBound))
    await first.stop()

    second = await start(
        tmp_path, agent_id=CHILD, registry=registry, sdk=sdk, fork_source_agent_id=SOURCE
    )
    try:
        options = sdk.clients[-1].options
        assert options.resume == fork_session_id(CHILD)
        assert options.fork_session is False
        assert options.session_id is None
    finally:
        await second.stop()


async def test_a_crash_after_the_clone_but_before_the_registry_write_resumes_it(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    sdk = FakeSDK()
    await _source(tmp_path, registry, sdk)
    clone = fork_session_id(CHILD)
    sdk.stored_sessions.add(clone)
    assert registry.load(CHILD, harness="claude_code") is None

    child = await start(
        tmp_path, agent_id=CHILD, registry=registry, sdk=sdk, fork_source_agent_id=SOURCE
    )
    try:
        options = sdk.clients[-1].options
        assert options.resume == fork_session_id(CHILD)
        assert options.fork_session is False
        await child.wait_for(lambda event: isinstance(event, SessionBound))
        record = registry.load(CHILD, harness="claude_code")
        assert record is not None and record.session_id == fork_session_id(CHILD)
    finally:
        await child.stop()


async def test_a_fork_whose_source_has_no_session_here_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    with pytest.raises(ForkSourceUnavailableError, match="holds no CLI session"):
        await start(tmp_path, agent_id=CHILD, registry=registry, fork_source_agent_id=SOURCE)


def _record(agent_id: int, harness: str, session: str, tmp_path: Path) -> SessionRecord:
    return SessionRecord(
        agent_id=agent_id,
        session_id=session,
        cwd=str(tmp_path),
        model="m",
        harness=harness,
        created_at="2026-11-11T00:00:00+00:00",
        updated_at="2026-11-11T00:00:00+00:00",
    )


async def test_a_child_coming_back_to_the_harness_resumes_its_old_record(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    sdk = FakeSDK()
    await _source(tmp_path, registry, sdk)
    first = await start(
        tmp_path, agent_id=CHILD, registry=registry, sdk=sdk, fork_source_agent_id=SOURCE
    )
    await first.wait_for(lambda event: isinstance(event, SessionBound))
    await first.stop()
    registry.save(_record(CHILD, "codex", "thread-elsewhere", tmp_path))

    back = await start(
        tmp_path, agent_id=CHILD, registry=registry, sdk=sdk, fork_source_agent_id=SOURCE
    )
    try:
        options = sdk.clients[-1].options
        assert options.resume == fork_session_id(CHILD)
        assert options.fork_session is False
        other = registry.load(CHILD, harness="codex")
        assert other is not None and other.session_id == "thread-elsewhere"
    finally:
        await back.stop()


async def test_a_source_session_kept_on_another_harness_is_not_cloned(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    registry.save(_record(SOURCE, "codex", "thread-old", tmp_path))
    with pytest.raises(ForkSourceUnavailableError, match="holds no CLI session"):
        await start(tmp_path, agent_id=CHILD, registry=registry, fork_source_agent_id=SOURCE)
