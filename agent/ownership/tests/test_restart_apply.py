# meiva:note
# What applying a restart writes beyond releasing the incarnation. A command without a transition
# is the native restart and keeps its note rule; one with a transition always leaves a note and
# stores only what changed.

from agent.ownership.lifecycle_plan import (
    RestartApply,
    RestartInputs,
    ResumeNote,
    restart_apply,
    resume_note,
)
from base.agents.epochs.model import RecordExtent, SwitchExtents
from base.agents.incarnation.restart_transition import RestartTransition


def _apply(
    agent_id: int,
    *,
    source: str,
    message: str | None,
    resume_turn: bool,
    transition: RestartTransition | None,
    stored_harness: str,
    extents: SwitchExtents | None = None,
) -> RestartApply:
    return restart_apply(
        agent_id,
        RestartInputs(source, message, resume_turn, transition),
        stored_harness=stored_harness,
        extents=extents,
    )


def test_a_restart_without_a_transition_writes_nothing_and_keeps_the_native_note_rule() -> None:
    plan = _apply(
        7, source="agent:3", message=None, resume_turn=True, transition=None, stored_harness="meiva"
    )
    assert plan.statements == ()
    assert plan.note == resume_note("agent:3", resume_turn=True)
    assert not plan.switches_harness
    idle = _apply(
        7, source="user", message=None, resume_turn=False, transition=None, stored_harness="meiva"
    )
    assert idle.note is None


def test_a_switch_stores_the_overlay_then_the_harness_and_says_where_the_agent_came_from() -> None:
    plan = _apply(
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
        (None, None, 7),
        (7, "codex", "gpt-6.1-sol", None, None, "self", "needs a shell", 7),
        ("codex", 7),
        (7,),
    ]
    assert plan.note == ResumeNote(
        content=(
            "[Harness switched from 'meiva' to 'codex' by yourself]\n"
            "Nothing said before the switch is visible here; this message is the handoff."
            "\n\nneeds a shell"
        ),
        source="self",
        note_tag="lifecycle_restart",
    )


def test_a_switch_to_the_native_harness_does_not_mark_a_harness_model_selection() -> None:
    plan = _apply(
        7,
        source="self",
        message="back",
        resume_turn=True,
        transition=RestartTransition("meiva", {}),
        stored_harness="codex",
    )

    assert [params for _sql, params in plan.statements] == [
        (None, None, 7),
        (7, "meiva", None, None, None, "self", "back", 7),
        ("meiva", 7),
    ]
    assert plan.note is not None
    assert plan.note.content.startswith("[Harness switched from 'codex' to 'meiva' by yourself]\n")
    assert plan.note.content.endswith("\n\nback")


def test_a_restart_that_keeps_the_harness_stores_only_the_overlay_and_leaves_a_plain_note() -> None:
    plan = _apply(
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


def test_restart_apply_announces_fast_acknowledgement_overlay_changes() -> None:
    ext_transition_off = _apply(
        7,
        source="self",
        message=None,
        resume_turn=False,
        transition=RestartTransition("codex", {"fast_acknowledgement": False}),
        stored_harness="codex",
    )
    assert ext_transition_off.note is not None
    assert "[Config updated: fast_acknowledgement=off]" in ext_transition_off.note.content

    ext_transition_on = _apply(
        7,
        source="user",
        message="enable fast ack",
        resume_turn=True,
        transition=RestartTransition("claude_code", {"fast_acknowledgement": True}),
        stored_harness="claude_code",
    )
    assert ext_transition_on.note is not None
    assert "[Config updated: fast_acknowledgement=on]" in ext_transition_on.note.content


def test_a_switch_between_external_harnesses_carries_where_each_record_ends_and_begins() -> None:
    plan = _apply(
        7,
        source="user",
        message=None,
        resume_turn=False,
        transition=RestartTransition("codex", {"llm_model": "gpt-6.1-sol"}),
        stored_harness="claude_code",
        extents=SwitchExtents(
            leaving=RecordExtent("claude-session", 900), arriving=RecordExtent("codex-session", 40)
        ),
    )

    assert [params for _sql, params in plan.statements][1:3] == [
        (900, "claude-session", 7),
        (7, "codex", "gpt-6.1-sol", "codex-session", 40, "user", None, 7),
    ]
