from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.lm.harness.base_session import ForkSourceUnavailableError
from base.lm.harness.codex.tests.fake_app_server import FakeAppServer
from base.lm.harness.codex.tests.support import handoff, start
from base.lm.harness.events import HandoffInjected, SessionBound
from base.lm.harness.registry import FileSessionRegistry, SessionRecord

SOURCE = 7
CHILD = 8


def _register(registry: FileSessionRegistry, agent_id: int, thread: str, cwd: Path) -> None:
    now = datetime.now(UTC).isoformat(timespec="seconds")
    registry.save(
        SessionRecord(
            agent_id=agent_id,
            session_id=thread,
            cwd=str(cwd),
            model="",
            created_at=now,
            updated_at=now,
        )
    )


async def test_first_start_clones_the_sources_thread_then_resumes_the_clone(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    _register(registry, SOURCE, "thread-src", tmp_path / "source-workspace")
    server = FakeAppServer(fork_thread_id="thread-clone")
    harness = await start(
        tmp_path,
        server=server,
        registry=registry,
        agent_id=CHILD,
        fork_source_agent_id=SOURCE,
        handoff_doc=handoff(CHILD),
    )
    try:
        assert server.methods().index("thread/fork") < server.methods().index("thread/resume")
        assert "thread/start" not in server.methods()
        fork_params = server.params_for("thread/fork")[0]
        assert fork_params == {
            "threadId": "thread-src",
            "cwd": str(tmp_path),
            "excludeTurns": True,
        }
        resume_params = server.params_for("thread/resume")[0]
        assert resume_params["threadId"] == "thread-clone"
        assert resume_params["cwd"] == str(tmp_path)
        assert resume_params["sandbox"] == "danger-full-access"
        bound = await harness.wait_for(lambda event: isinstance(event, SessionBound))
        assert bound.session_id == "thread-clone"
        assert bound.resumed is True
        assert harness.of(HandoffInjected) == []
        record = registry.load(CHILD)
        assert record is not None and record.session_id == "thread-clone"
        source = registry.load(SOURCE)
        assert source is not None and source.session_id == "thread-src"
    finally:
        await harness.stop()


async def test_the_clone_is_registered_before_the_thread_is_resumed(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    _register(registry, SOURCE, "thread-src", tmp_path)
    server = FakeAppServer(fork_thread_id="thread-clone", resume_fails=True)
    with pytest.raises(Exception, match="thread not found"):
        await start(
            tmp_path, server=server, registry=registry, agent_id=CHILD, fork_source_agent_id=SOURCE
        )
    record = registry.load(CHILD)
    assert record is not None and record.session_id == "thread-clone"


async def test_a_restart_resumes_the_registered_clone_without_forking_again(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    _register(registry, SOURCE, "thread-src", tmp_path)
    _register(registry, CHILD, "thread-clone", tmp_path)
    server = FakeAppServer()
    harness = await start(
        tmp_path, server=server, registry=registry, agent_id=CHILD, fork_source_agent_id=SOURCE
    )
    try:
        assert "thread/fork" not in server.methods()
        assert server.params_for("thread/resume")[0]["threadId"] == "thread-clone"
    finally:
        await harness.stop()


async def test_a_fork_whose_source_has_no_session_here_is_refused(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    server = FakeAppServer()
    with pytest.raises(ForkSourceUnavailableError, match="holds no CLI session"):
        await start(
            tmp_path, server=server, registry=registry, agent_id=CHILD, fork_source_agent_id=SOURCE
        )
    assert "thread/start" not in server.methods()
