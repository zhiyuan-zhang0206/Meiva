from __future__ import annotations

from pathlib import Path

from base.lm.harness.antigravity.tests.support import start


def denied_turn() -> dict[str, object]:
    return {
        "steps": [
            {
                "step_index": 1,
                "state": "ACTIVE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}},
            },
            {
                "step_index": 1,
                "state": "DONE",
                "step_type": "tool",
                "tool_name": "run_command",
                "tool_info": {"name": "run_command", "parameters": {"CommandLine": "ls"}},
            },
        ],
        "result": {
            "status": "SUCCESS",
            "response": "",
            "denied_actions": [{"action": "command", "display_name": "RunCommand"}],
        },
    }


async def test_a_denied_action_is_a_failed_turn_even_though_the_cli_says_success(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path, turns=[denied_turn()])
    try:
        outcome = await harness.session.submit("list files").outcome()
        assert outcome.is_error is True
        assert outcome.subtype == "denied"
        assert outcome.permission_denials == ("command:RunCommand",)
        assert outcome.text == ""
    finally:
        await harness.stop()


async def test_a_clean_turn_reports_no_denials(tmp_path: Path) -> None:
    harness = await start(
        tmp_path,
        turns=[{"steps": [], "result": {"status": "SUCCESS", "response": "ok"}}],
    )
    try:
        outcome = await harness.session.submit("hi").outcome()
        assert outcome.is_error is False
        assert outcome.permission_denials == ()
    finally:
        await harness.stop()
