# meiva:note
# What applying a restart writes beyond releasing the incarnation. A command without a transition
# is the native restart and keeps its note rule; one with a transition always leaves a note and
# stores only what changed.

from agent.ownership.lifecycle_plan import ResumeNote, restart_apply, resume_note
from base.agents.incarnation.restart_transition import RestartTransition


def test_a_restart_without_a_transition_writes_nothing_and_keeps_the_native_note_rule() -> None:
    plan = restart_apply(
        7, source="agent:3", message=None, resume_turn=True, transition=None, stored_harness="meiva"
    )
    assert plan.statements == ()
    assert plan.note == resume_note("agent:3", resume_turn=True)
    assert not plan.switches_harness
    idle = restart_apply(
        7, source="user", message=None, resume_turn=False, transition=None, stored_harness="meiva"
    )
    assert idle.note is None


def test_a_switch_stores_the_overlay_then_the_harness_and_says_where_the_agent_came_from() -> None:
    plan = restart_apply(
        7,
        source="self",
        message="needs a shell",
        resume_turn=True,
        transition=RestartTransition("codex", {"llm_model": "gpt-6.1-sol"}),
        stored_harness="meiva",
    )

    assert plan.switches_harness
    assert [params for _sql, params in plan.statements] == [
        ('{"llm_model": "gpt-6.1-sol"}', 7),
        ("codex", 7),
        (7,),
    ]
    assert plan.note == ResumeNote(
        content="[Harness switched from 'meiva' to 'codex']\nneeds a shell",
        source="self",
        note_tag="lifecycle_restart",
    )


def test_a_switch_to_the_native_harness_does_not_mark_a_harness_model_selection() -> None:
    plan = restart_apply(
        7,
        source="self",
        message="back",
        resume_turn=True,
        transition=RestartTransition("meiva", {}),
        stored_harness="codex",
    )

    assert [params for _sql, params in plan.statements] == [("meiva", 7)]
    assert plan.note is not None
    assert plan.note.content == "[Harness switched from 'codex' to 'meiva']\nback"


def test_a_restart_that_keeps_the_harness_stores_only_the_overlay_and_leaves_a_plain_note() -> None:
    plan = restart_apply(
        7,
        source="self",
        message="shorter timeout",
        resume_turn=False,
        transition=RestartTransition("codex", {"exec_timeout_seconds": 30}),
        stored_harness="codex",
    )

    assert not plan.switches_harness
    assert [params for _sql, params in plan.statements] == [('{"exec_timeout_seconds": 30}', 7)]
    assert plan.note is not None
    assert plan.note.content == "Restart was accepted from yourself: shorter timeout"
