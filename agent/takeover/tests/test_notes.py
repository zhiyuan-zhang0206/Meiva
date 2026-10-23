from datetime import UTC, datetime

from agent.takeover.notes import (
    INTRODUCTION_ID,
    NoteStamp,
    SessionRef,
    handoff_draft,
    handoff_note_id,
    handoff_receipt,
    handoff_summary,
    introduction_draft,
    start_drafts,
)

MOMENT = datetime(2026, 10, 6, 13, 39, 25, tzinfo=UTC)
PLAIN = NoteStamp(MOMENT, "")
STAMPED = NoteStamp(MOMENT, "[2026-10-06 Tue 09:39:25] ")
REF = SessionRef(agent_id=42, session_id=0, name="Continue the task")


def test_the_session_ref_is_read_from_a_lease_row_and_refuses_a_malformed_one() -> None:
    lease = {"agent_id": 42, "session_id": 7, "name": "Fix login", "id": "ignored"}
    assert SessionRef.of(lease) == SessionRef(agent_id=42, session_id=7, name="Fix login")
    try:
        SessionRef.of({**lease, "session_id": "7"})
    except TypeError:
        return
    raise AssertionError("a lease whose session id is text must be refused")


def test_the_first_takeover_explains_borrowed_identity_before_the_start_marker() -> None:
    introduction, start = start_drafts(REF, PLAIN, introduced=False, existing_ids=())
    assert introduction.note_id == INTRODUCTION_ID
    assert "You are paused" in introduction.text
    assert "receives incoming messages" in introduction.text
    assert "coming from you, an Meiva agent" in introduction.text
    assert "continue any unfinished requests" in introduction.text
    assert start.note_id == "impersonation-start:42:0"
    assert start.anchors_session
    assert start.text == (
        "Impersonation session 0 has started. "
        'An external executor named "Continue the task" is taking over your work. '
        "You are paused until it returns control."
    )
    assert not introduction.anchors_session


def test_later_takeovers_do_not_repeat_the_introduction() -> None:
    assert [d.note_id for d in start_drafts(REF, PLAIN, introduced=True, existing_ids=())] == [
        "impersonation-start:42:0"
    ]


def test_notes_the_checkpoint_already_holds_are_not_written_again() -> None:
    held = {INTRODUCTION_ID, "impersonation-start:42:0"}
    assert not start_drafts(REF, PLAIN, introduced=False, existing_ids=held)
    only_start_missing = start_drafts(REF, PLAIN, introduced=False, existing_ids={INTRODUCTION_ID})
    assert [d.note_id for d in only_start_missing] == ["impersonation-start:42:0"]


def test_every_note_carries_the_stamp_prefix_and_its_creation_instant() -> None:
    drafts = start_drafts(REF, STAMPED, introduced=False, existing_ids=())
    drafts += (
        handoff_draft(
            REF, STAMPED, summary="Finished", path="/w/impersonation/0.json", reason=None
        ),
    )
    assert [d.note_id for d in drafts] == [
        INTRODUCTION_ID,
        "impersonation-start:42:0",
        "impersonation-handoff:42:0",
    ]
    for draft in drafts:
        assert draft.text.startswith("[2026-10-06 Tue 09:39:25] ")
        assert draft.created_at == MOMENT
    assert introduction_draft(PLAIN).text.startswith("Impersonation means")


def test_the_handoff_note_names_the_summary_the_record_and_the_reply_channel() -> None:
    draft = handoff_draft(REF, PLAIN, summary="Fixed login", path="/w/0.json", reason=None)
    assert "Impersonation session 0 has ended. You have resumed execution" in draft.text
    assert "External summary:\nFixed login" in draft.text
    assert "The session record is available at: /w/0.json" in draft.text
    assert "Reply to the user in ordinary assistant text in this conversation" in draft.text
    assert "missing entries do not establish that an action never happened" in draft.text
    assert "stopped early" not in draft.text


def test_a_supervisor_aborted_session_names_its_death_cause_after_the_end_sentence() -> None:
    draft = handoff_draft(
        REF, PLAIN, summary="s", path="/w/0.json", reason="the bound relay process exited"
    )
    assert (
        "Impersonation session 0 has ended. This session was stopped early: "
        "the bound relay process exited. You have resumed" in draft.text
    )


def test_the_receipt_and_note_id_are_derived_from_the_session() -> None:
    assert handoff_receipt(REF) == "42:0"
    assert handoff_note_id("42:0") == "impersonation-handoff:42:0"


def test_a_missing_external_summary_is_replaced_by_the_session_end_facts() -> None:
    assert handoff_summary("Done", status="released", rejection_reason=None) == "Done"
    assert handoff_summary("", status="released", rejection_reason=None) == ""
    assert handoff_summary(None, status="expired", rejection_reason="aborted: x") == (
        "Session ended with status expired. aborted: x No external completion summary was supplied."
    )
    assert handoff_summary(None, status="released", rejection_reason=None) == (
        "Session ended with status released.  No external completion summary was supplied."
    )
