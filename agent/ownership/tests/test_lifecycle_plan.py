from uuid import UUID

import pytest

from agent.ownership.lifecycle_plan import (
    ResumeNote,
    TurnClose,
    lifecycle_kind,
    owns_inbound_row,
    resume_note,
    shell_cutoff,
    turn_close,
)
from base.agents.messages.lifecycle_text import restart_resume_note
from base.native_process.runtime_incarnation import RuntimeIncarnation


@pytest.mark.parametrize("kind", ["restart", "terminate"])
def test_only_restart_and_terminate_are_executable(kind: str) -> None:
    assert lifecycle_kind(kind) == kind


@pytest.mark.parametrize("kind", ["cancel", "chat", ""])
def test_any_other_kind_is_refused(kind: str) -> None:
    with pytest.raises(ValueError, match="not an executable lifecycle command"):
        lifecycle_kind(kind)


def test_a_restart_that_ended_an_unfinished_turn_queues_the_continuing_note() -> None:
    assert resume_note("agent:7", resume_turn=True) == ResumeNote(
        content=restart_resume_note("agent:7"), source="agent:7", note_tag="lifecycle_restart"
    )


def test_a_restart_of_an_idle_agent_queues_no_note() -> None:
    assert resume_note("user", resume_turn=False) is None


@pytest.mark.parametrize(
    ("kill_flags", "expected"),
    [([], None), ([False, False], None), ([False, True], 7), ([True], 7)],
)
def test_the_shell_cutoff_exists_only_when_a_closed_terminate_asked_for_the_kill(
    kill_flags: list[bool], expected: int | None
) -> None:
    assert shell_cutoff(kill_flags, 7) == expected


@pytest.mark.parametrize(
    ("exited", "crashed", "expected"),
    [
        (False, False, TurnClose(stamp_fatal=False, settle=True)),
        (False, True, TurnClose(stamp_fatal=True, settle=True)),
        (True, False, TurnClose(stamp_fatal=False, settle=False)),
        (True, True, TurnClose(stamp_fatal=True, settle=False)),
    ],
)
def test_closing_a_turn_stamps_a_crash_and_settles_unless_the_host_is_exiting(
    exited: bool, crashed: bool, expected: TurnClose
) -> None:
    assert turn_close(exited=exited, crashed=crashed) == expected


def test_owns_inbound_row_requires_matching_incarnation_or_unowned_row() -> None:
    generation = UUID(int=1)
    owner = UUID(int=2)
    incarnation = RuntimeIncarnation(agent_id=7, generation=generation, owner=owner)

    assert not owns_inbound_row(None, incarnation=None)
    assert owns_inbound_row((None, None, "idling"), incarnation=None)
    assert not owns_inbound_row((generation, owner, "running"), incarnation=None)
    assert owns_inbound_row((generation, owner, "running"), incarnation=incarnation)
    assert owns_inbound_row((generation, owner, "idling"), incarnation=incarnation)
    assert not owns_inbound_row((generation, owner, "terminated"), incarnation=incarnation)
    assert not owns_inbound_row((UUID(int=3), owner, "running"), incarnation=incarnation)


def test_owns_inbound_row_refuses_a_row_of_the_wrong_shape() -> None:
    with pytest.raises(ValueError, match="got a row of 2 columns"):
        owns_inbound_row((None, None), incarnation=None)
