# meiva:note
# What a Claude session needs to take part in reconciliation after a restart: the generation counts on
# across sessions (it is part of the registry record), the session reads the transcript of the session
# its record names under the home it was given, and a record it cannot read is an error, not NOT_FOUND.
# meiva:link base/lm/harness/docs/delivery.md

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from base.lm.harness import FileSessionRegistry
from base.lm.harness.claude.tests.support import start
from base.lm.harness.delivery import Reconciliation, ReconciliationUnavailableError
from base.lm.harness.events import AckSignal, InputReceived, SessionBound

TRANSCRIPT = Path(__file__).parents[1] / "reconciliation" / "tests" / "fixtures" / "session-1.jsonl"
AGENT = 7


async def _lifetime(tmp_path: Path, registry: FileSessionRegistry, home: Path) -> int:
    run = await start(
        tmp_path,
        agent_id=AGENT,
        registry=registry,
        session_id="session-1",
        environ={"HOME": str(home)},
    )
    await run.wait_for(lambda event: isinstance(event, SessionBound))
    generation = run.session.generation
    await run.stop()
    return generation


async def test_the_generation_keeps_growing_across_sessions_through_the_record(
    tmp_path: Path,
) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")

    assert [await _lifetime(tmp_path, registry, tmp_path) for _ in range(3)] == [1, 2, 3]
    record = registry.load(AGENT, harness="claude_code")
    assert record is not None and record.generation == 3


async def test_a_session_reads_the_transcript_of_its_recorded_session(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    home = tmp_path / "home"
    _ = await _lifetime(tmp_path, registry, home)
    target = home / ".claude" / "projects" / "-workspace" / "session-1.jsonl"
    target.parent.mkdir(parents=True)
    _ = shutil.copy(TRANSCRIPT, target)
    run = await start(tmp_path, agent_id=AGENT, registry=registry, environ={"HOME": str(home)})
    try:
        answers = await run.session.reconcile(["k-absorbed", "k-enqueued"])

        assert answers == {
            "k-absorbed": Reconciliation.IN_CONTEXT,
            "k-enqueued": Reconciliation.NOT_FOUND,
        }
    finally:
        await run.stop()


async def test_a_missing_transcript_is_unavailable_never_not_found(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "sessions")
    home = tmp_path / "home"
    _ = await _lifetime(tmp_path, registry, home)
    run = await start(tmp_path, agent_id=AGENT, registry=registry, environ={"HOME": str(home)})
    try:
        with pytest.raises(ReconciliationUnavailableError):
            _ = await run.session.reconcile(["k-absorbed"])
    finally:
        await run.stop()


async def test_a_session_with_no_record_has_nothing_to_reconcile_against(tmp_path: Path) -> None:
    run = await start(tmp_path, environ={"HOME": str(tmp_path)})
    try:
        with pytest.raises(ReconciliationUnavailableError):
            _ = await run.session.reconcile(["k"])
    finally:
        await run.stop()


async def test_an_adopted_input_is_announced_received_once(tmp_path: Path) -> None:
    run = await start(tmp_path, environ={"HOME": str(tmp_path)})
    try:
        run.session.adopt(["k-stored"])
        run.session.adopt(["k-stored"])

        received = await run.wait_for(lambda event: isinstance(event, InputReceived))
        assert (received.key, received.signal, received.generation) == (
            "k-stored",
            AckSignal.WEAK,
            1,
        )
        assert len(run.of(InputReceived)) == 1, "adopting twice announces once"
    finally:
        await run.stop()
