from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from meiva.shell.coding_tools.plans.claude_launch import claude_command
from meiva.shell.coding_tools.plans.claude_screen import claude_ui_ready
from meiva.shell.coding_tools.plans.launch_request import worker_bootstrap

_SKILL_DIR = Path(__file__).parents[1]
_SESSION = "01a0e1ac-adc7-7d33-bd13-8ce2c6a686c5"


def test_claude_pins_or_reopens_the_session_id(tmp_path: Path) -> None:
    fresh = claude_command(tmp_path, claude_session=_SESSION)
    resumed = claude_command(tmp_path, claude_session=_SESSION, resume=True)

    assert fresh.endswith(f"--dangerously-skip-permissions --session-id {_SESSION} || exit $?")
    assert resumed.endswith(f"--dangerously-skip-permissions --resume {_SESSION} || exit $?")


_RESUMED_PANEL = (
    "\u23fa takeover active\n"
    "\u273b Crunched for 59s\n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "\u276f \n"
    "\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\n"
    "  \u23f5\u23f5 bypass permissions on (shift+tab to cycle) \u00b7 \u2190 for agents\n"
)


def test_a_resumed_panel_is_ready_without_the_welcome_banner() -> None:
    shell = "host:cl-b user$ exec claude --dangerously-skip-permissions --resume x\n"

    assert not claude_ui_ready(_RESUMED_PANEL)
    assert claude_ui_ready(_RESUMED_PANEL, resumed=True)
    assert not claude_ui_ready(shell, resumed=True)


def test_a_fresh_worker_message_does_not_mention_an_interruption(tmp_path: Path) -> None:
    message = worker_bootstrap(tmp_path / "c.md", tmp_path, tmp_path / "t.md", tmp_path / "w.md")
    assert "interrupted" not in message
    assert message.endswith("Now read the task file and start working.")


@pytest.mark.parametrize("script", ["spawn_claude", "spawn_codex"])
@pytest.mark.parametrize(
    "extra", [["--resume", "not-a-session"], ["--resume", _SESSION, "--status"]]
)
def test_resume_takes_a_session_uuid_and_excludes_status(
    script: str, extra: list[str], tmp_path: Path
) -> None:
    spec = importlib.util.spec_from_file_location(
        f"resume_{script}", _SKILL_DIR / "scripts" / f"{script}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(SystemExit) as excinfo:
        module.main([str(tmp_path), *extra])
    assert excinfo.value.code == 2
