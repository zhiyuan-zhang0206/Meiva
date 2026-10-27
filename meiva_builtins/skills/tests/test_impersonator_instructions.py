from __future__ import annotations

from datetime import UTC, datetime
from types import ModuleType
from typing import Any

import psycopg
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

import meiva
from agent.tests.impersonation.external.runtime import Takeover
from agent.tests.impersonation.external.runtime import attached_runtime as attached_runtime
from agent.tests.impersonation.external.runtime import cluster as cluster
from agent.tests.impersonation.external.runtime import prior_context as prior_context
from agent.tests.impersonation.external.runtime import sample_surface as sample_surface
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.config_authority import config_authority as config_authority
from base.packages.tests.model_catalog import model_catalog as model_catalog
from meiva_builtins.skills.tests.skill_script import load_skill_script

_PICKED_UP_AT = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def sdk_identity() -> ModuleType:
    return meiva


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
        system_note_message(content="Memory index", tag=NoteTag.MEMORY, picked_up_at=_PICKED_UP_AT),
        system_note_message(
            content="Preloaded skill", tag=NoteTag.PRELOADED_SKILLS, picked_up_at=_PICKED_UP_AT
        ),
        HumanMessage(content="Task history"),
        system_note_message(
            content="Fork-added skill", tag=NoteTag.PRELOADED_SKILLS, picked_up_at=_PICKED_UP_AT
        ),
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


# meiva:note
# The real attachment reads valid native instructions, then Postgres refuses its receipt seal.
# Closing must fail before main can publish the text; an early attach refusal cannot prove this.
def test_instruction_script_prints_only_after_successful_detach(
    instruction_reader: Any, attached_runtime: Takeover, capsys: pytest.CaptureFixture[str]
) -> None:
    attached_runtime.write_checkpoint(messages=[SystemMessage(content="Native rules")])
    db = attached_runtime.cluster.db_conn
    db.execute(
        "CREATE FUNCTION refuse_instruction_detach() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'instruction detach refused'; END $$"
    )
    db.execute(
        "CREATE TRIGGER refuse_instruction_detach BEFORE UPDATE "
        "ON agent_impersonation_event_participants FOR EACH ROW "
        "WHEN (NEW.state = 'sealed') EXECUTE FUNCTION refuse_instruction_detach()"
    )
    db.commit()
    try:
        with pytest.raises(psycopg.errors.RaiseException, match="instruction detach refused"):
            instruction_reader.main(
                [
                    str(attached_runtime.lease()["session_id"]),
                    "--agent",
                    str(attached_runtime.agent_id),
                ]
            )
        assert capsys.readouterr().out == ""
        assert attached_runtime.receipts() == ["open"]
    finally:
        db.rollback()
        db.execute(
            "DROP TRIGGER refuse_instruction_detach ON agent_impersonation_event_participants"
        )
        db.execute("DROP FUNCTION refuse_instruction_detach()")
        db.commit()
