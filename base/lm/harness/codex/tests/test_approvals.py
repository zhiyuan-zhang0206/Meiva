from __future__ import annotations

import asyncio
from pathlib import Path

from base.lm.harness.codex.tests.support import start
from base.lm.harness.events import TurnCompleted

COMMAND = "rm -rf /tmp/definitely-not-needed"


async def test_declined_command_is_reported_by_the_model(tmp_path: Path) -> None:
    harness = await start(tmp_path, approval_policy="untrusted", approval_decision="decline")
    try:
        ticket = harness.session.submit("delete the temp directory")
        await harness.turn_started()
        answer = await harness.server.ask(
            "item/commandExecution/requestApproval",
            {
                "threadId": "thread-1",
                "turnId": harness.server.current_turn,
                "itemId": "exec-1",
                "startedAtMs": 0,
                "command": COMMAND,
            },
        )
        assert answer == {"decision": "decline"}
        harness.server.message("The command was rejected, so I did not run it.")
        harness.server.complete()
        outcome = await asyncio.wait_for(ticket.outcome(), 2)
        assert "did not run it" in outcome.text
        assert outcome.permission_denials == (f"command: {COMMAND}",)
        completed = await harness.wait_for(lambda event: isinstance(event, TurnCompleted))
        assert completed.permission_denials == (f"command: {COMMAND}",)
    finally:
        await harness.stop()


async def test_accepted_command_is_answered_with_accept(tmp_path: Path) -> None:
    harness = await start(tmp_path, approval_policy="untrusted", approval_decision="accept")
    try:
        harness.session.submit("delete the temp directory")
        await harness.turn_started()
        answer = await harness.server.ask(
            "item/commandExecution/requestApproval", {"command": COMMAND}
        )
        assert answer == {"decision": "accept"}
    finally:
        await harness.stop()


async def test_file_change_approval_uses_the_same_decision(tmp_path: Path) -> None:
    harness = await start(tmp_path, approval_policy="untrusted", approval_decision="decline")
    try:
        harness.session.submit("edit the file")
        await harness.turn_started()
        answer = await harness.server.ask("item/fileChange/requestApproval", {"itemId": "change-1"})
        assert answer == {"decision": "decline"}
    finally:
        await harness.stop()


async def test_legacy_patch_approval_uses_its_own_vocabulary(tmp_path: Path) -> None:
    harness = await start(tmp_path, approval_policy="untrusted", approval_decision="accept")
    try:
        harness.session.submit("apply the patch")
        await harness.turn_started()
        answer = await harness.server.ask("applyPatchApproval", {"callId": "call-1"})
        assert answer == {"decision": "approved"}
    finally:
        await harness.stop()


async def test_permission_requests_grant_nothing(tmp_path: Path) -> None:
    harness = await start(tmp_path, approval_policy="untrusted")
    try:
        harness.session.submit("ask for more")
        await harness.turn_started()
        answer = await harness.server.ask(
            "item/permissions/requestApproval", {"cwd": str(tmp_path), "permissions": {}}
        )
        assert answer == {"permissions": {}, "scope": "turn"}
    finally:
        await harness.stop()
