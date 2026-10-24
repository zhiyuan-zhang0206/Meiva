from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from meiva_builtins.skills.tests.skill_script import load_skill_script


@pytest.fixture
def instruction_reader() -> Any:
    return load_skill_script(
        "platform",
        "impersonator-guide",
        "scripts",
        "read_instructions.py",
        name="impersonator_instruction_reader",
    )


def test_instruction_script_selects_native_prompt_and_preloaded_skills(
    instruction_reader: Any,
) -> None:
    from agent.messages import NoteTag, system_note_message

    messages = [
        SystemMessage(content="Native core and plugin rules\n"),
        system_note_message(content="Memory index", tag=NoteTag.MEMORY),
        system_note_message(content="Preloaded skill", tag=NoteTag.PRELOADED_SKILLS),
        HumanMessage(content="Task history"),
        system_note_message(content="Fork-added skill", tag=NoteTag.PRELOADED_SKILLS),
    ]
    assert instruction_reader.instruction_text(messages) == (
        "Native core and plugin rules\n\n\n"
        "<system_note>\nPreloaded skill\n</system_note>\n\n"
        "<system_note>\nFork-added skill\n</system_note>"
    )


@pytest.mark.parametrize("content", [None, "", [{"type": "text", "text": "rules"}]])
def test_instruction_script_refuses_missing_or_nontext_prompt(
    instruction_reader: Any, content: Any
) -> None:
    messages = [] if content is None else [SystemMessage(content=content)]
    with pytest.raises((RuntimeError, ValueError), match=r"system prompt|nonempty text"):
        instruction_reader.instruction_text(messages)


def test_instruction_script_reads_the_target_from_the_command_line(
    instruction_reader: Any,
) -> None:
    target = instruction_reader.parse_target(["0", "--agent", "405"])
    assert (target.session_id, target.agent_id) == (0, 405)


def test_instruction_script_requires_the_agent(
    instruction_reader: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        instruction_reader.parse_target(["0"])
    assert exit_info.value.code == 2
    assert "--agent" in capsys.readouterr().err


# meiva:note
# Against a real throwaway database no agent holds a lease, so the real attach refuses before
# anything is read; nothing may reach stdout.
def test_instruction_script_prints_nothing_when_the_attachment_is_refused(
    instruction_reader: Any, db_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(ValueError, match="no impersonation session 0"):
        instruction_reader.main(["0", "--agent", "405"])
    assert capsys.readouterr().out == ""
