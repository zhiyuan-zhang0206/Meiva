from __future__ import annotations

import asyncio
from pathlib import Path

from base.lm.harness.codex.tests.fake_app_server import FakeAppServer
from base.lm.harness.codex.tests.support import Harness, start
from base.lm.harness.delivery.tests.sent import send
from base.lm.harness.events import UnsolicitedOutput
from base.lm.harness.registry import FileSessionRegistry
from base.lm.harness.session import BackgroundKind


# meiva:note
# The item shape is the app-server schema's `commandExecution` thread item. The recorded real run
# (a `unified_exec` command started with a short yield) shows the turn completing while the command's
# item never completed, and the process living under the app-server until it exited; no recording
# shows what the app-server says when such a command ends later, which is why the session also checks
# the process tree.
# meiva:link base/lm/harness/codex/docs/codex.md
def command_item(
    item_id: str = "call-1",
    *,
    command: str = "sleep 3917",
    source: str = "unifiedExecStartup",
    status: str = "inProgress",
) -> dict[str, object]:
    return {
        "type": "commandExecution",
        "id": item_id,
        "command": command,
        "cwd": "/w",
        "source": source,
        "status": status,
        "processId": "1001",
        "commandActions": [],
    }


async def stop(harness: Harness) -> None:
    # meiva:note
    # The scripted process the command stands for ends with the app-server, as a real one would;
    # left alive it is the residue the close path reports.
    harness.probe.reap(5001)
    await harness.stop()


def emit_item(harness: Harness, method: str, item: dict[str, object]) -> None:
    harness.server.emit(
        method,
        {"threadId": harness.server.thread_id, "turnId": harness.server.current_turn, "item": item},
    )


async def run_turn_starting(harness: Harness, item: dict[str, object] | None = None) -> None:
    turn = await send(harness, "start a server")
    await harness.turn_started(1)
    emit_item(harness, "item/started", item or command_item())
    harness.server.message("STARTED")
    harness.server.complete()
    _ = await asyncio.wait_for(turn.outcome(), 2)


async def test_a_command_still_running_when_its_turn_ends_is_background_work(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        await run_turn_starting(harness)
        harness.probe.spawn(5001, "sleep 3917")

        [item] = harness.session.background_work().items
        assert item.kind is BackgroundKind.COMMAND
        assert item.description == "sleep 3917"
    finally:
        await stop(harness)


async def test_a_completed_item_clears_the_command(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        await run_turn_starting(harness)
        harness.probe.spawn(5001, "sleep 3917")
        assert harness.session.background_work()

        emit_item(harness, "item/completed", command_item(status="completed"))
        await asyncio.sleep(0)

        assert not harness.session.background_work()
    finally:
        await stop(harness)


async def test_an_item_the_process_tree_does_not_back_is_not_kept_alive(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        await run_turn_starting(harness)

        assert not harness.session.background_work(), "no process beyond the app-server's own"
    finally:
        await stop(harness)


async def test_an_unobservable_app_server_is_taken_at_its_word(tmp_path: Path) -> None:
    harness = await start(tmp_path, server=FakeAppServer(pid=None))
    try:
        await run_turn_starting(harness)

        assert harness.session.background_work()
    finally:
        await stop(harness)


async def test_a_user_shell_command_or_a_poll_is_not_new_background_work(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        await run_turn_starting(harness, command_item(source="userShell"))
        harness.probe.spawn(5001, "sleep 3917")
        assert not harness.session.background_work()

        emit_item(harness, "item/started", command_item("call-2", source="unifiedExecInteraction"))
        await asyncio.sleep(0)
        assert not harness.session.background_work()
    finally:
        await stop(harness)


async def test_a_turn_that_ended_interrupted_took_its_commands_with_it(tmp_path: Path) -> None:
    harness = await start(tmp_path)
    try:
        turn = await send(harness, "start a server")
        await harness.turn_started(1)
        emit_item(harness, "item/started", command_item())
        harness.probe.spawn(5001, "sleep 3917")
        assert harness.session.background_work()

        harness.server.complete(status="interrupted")
        _ = await asyncio.wait_for(turn.outcome(), 2)

        assert not harness.session.background_work()
    finally:
        await stop(harness)


async def test_the_pending_command_is_recorded_for_the_next_process(tmp_path: Path) -> None:
    registry = FileSessionRegistry(tmp_path / "registry")
    harness = await start(tmp_path, agent_id=7, registry=registry)
    try:
        await run_turn_starting(harness)
        harness.probe.spawn(5001, "sleep 3917")
        emit_item(harness, "item/started", command_item("call-2", command="npm run dev"))
        await asyncio.sleep(0)
    finally:
        await stop(harness)

    stored = registry.load(7, harness="codex")
    assert stored is not None
    assert [item.description for item in stored.background] == ["sleep 3917", "npm run dev"]

    second = await start(tmp_path, agent_id=7, registry=registry)
    try:
        assert [item.description for item in second.session.take_lost_background()] == [
            "sleep 3917",
            "npm run dev",
        ]
    finally:
        await stop(second)


async def test_a_turn_the_server_starts_itself_is_observed_and_not_reported_as_unsolicited(
    tmp_path: Path,
) -> None:
    delivered: list[UnsolicitedOutput] = []
    harness = await start(tmp_path, unsolicited=delivered.append)
    try:
        harness.server.emit(
            "turn/started",
            {
                "threadId": harness.server.thread_id,
                "turn": {"id": "server-turn", "status": "inProgress"},
            },
        )
        await asyncio.sleep(0)
        assert harness.session.background_work(), "a turn the server is running is not idle"

        harness.server.message("TIMER-1", turn_id="server-turn")
        harness.server.complete(turn_id="server-turn")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        assert delivered == []
        assert not harness.session.background_work()
    finally:
        await stop(harness)
