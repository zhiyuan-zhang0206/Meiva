from __future__ import annotations

import pytest

from base.agents.messages.kwargs import NoteTag
from base.agents.messages.termination_notice import (
    CRASH_HARVEST_SOURCE,
    TERMINATION_MESSAGE_PAYLOAD_KEY,
    ancestor_termination_payload,
    format_ancestor_termination_notice,
    should_notify_ancestor,
    terminate_accepted_text,
    termination_actor_agent_id,
    termination_message_of,
)


@pytest.mark.parametrize(
    ("source", "terminated_id", "expected"),
    [
        ("self", 42, 42),
        ("agent:405", 42, 405),
        ("agent:42", 42, 42),
        ("user", 42, None),
        ("external_agent:claude_code", 42, None),
        (CRASH_HARVEST_SOURCE, 42, None),
        ("machine-pause", 42, None),
        ("system", 42, None),
    ],
)
def test_termination_actor_agent_id(source: str, terminated_id: int, expected: int | None) -> None:
    assert termination_actor_agent_id(source, terminated_agent_id=terminated_id) == expected


def test_should_notify_ancestor_skips_only_missing_or_self_echo() -> None:
    assert not should_notify_ancestor(None, source="self", terminated_agent_id=42)
    assert not should_notify_ancestor(None, source="user", terminated_agent_id=42)
    assert not should_notify_ancestor(405, source="agent:405", terminated_agent_id=42)
    assert should_notify_ancestor(405, source="self", terminated_agent_id=42)
    assert should_notify_ancestor(405, source="agent:42", terminated_agent_id=42)
    assert should_notify_ancestor(405, source="agent:999", terminated_agent_id=42)
    assert should_notify_ancestor(405, source="user", terminated_agent_id=42)
    assert should_notify_ancestor(405, source="external_agent:codex", terminated_agent_id=42)
    assert should_notify_ancestor(405, source=CRASH_HARVEST_SOURCE, terminated_agent_id=42)


def test_termination_message_extraction() -> None:
    assert termination_message_of(None, None) is None
    assert termination_message_of("", {}) is None
    assert termination_message_of("   ", {TERMINATION_MESSAGE_PAYLOAD_KEY: "   "}) is None
    assert termination_message_of("", {TERMINATION_MESSAGE_PAYLOAD_KEY: 123}) is None
    assert termination_message_of("", {TERMINATION_MESSAGE_PAYLOAD_KEY: "  done  "}) == "done"
    assert termination_message_of("  from self  ", {TERMINATION_MESSAGE_PAYLOAD_KEY: "other"}) == (
        "from self"
    )


def test_format_ancestor_termination_notice_includes_id_label_initiator_and_message() -> None:
    with_all = format_ancestor_termination_notice(
        terminated_agent_id=8528,
        label="step1-worker",
        source="self",
        message="  Draft PR ready  ",
    )
    assert (
        with_all == "Agent #8528 (step1-worker) terminated (by self).\n\nMessage:\nDraft PR ready"
    )

    by_peer = format_ancestor_termination_notice(
        terminated_agent_id=8528,
        label=None,
        source="agent:999",
        message=None,
    )
    assert by_peer == "Agent #8528 terminated (by agent #999)."

    by_user = format_ancestor_termination_notice(
        terminated_agent_id=8528,
        label="worker",
        source="user",
        message="Stopped from UI",
    )
    assert by_user == "Agent #8528 (worker) terminated (by user).\n\nMessage:\nStopped from UI"

    by_reaper = format_ancestor_termination_notice(
        terminated_agent_id=8528,
        label="worker",
        source=CRASH_HARVEST_SOURCE,
        message="RuntimeError: provider 500",
    )
    assert (
        by_reaper
        == "Agent #8528 (worker) terminated (by corpse_reaper).\n\nMessage:\nRuntimeError: provider 500"
    )


def test_ancestor_termination_payload_uses_agent_terminated_tag() -> None:
    assert ancestor_termination_payload(terminated_agent_id=8528, source="self") == {
        "note_tag": NoteTag.AGENT_TERMINATED.value,
        "terminated_agent_id": 8528,
        "termination_source": "self",
    }


def test_terminate_accepted_text_preserves_bare_and_messaged_forms() -> None:
    assert terminate_accepted_text("user") == "Termination was accepted from user"
    assert terminate_accepted_text("self") == "Termination was accepted from yourself"
    assert terminate_accepted_text("self", "   ") == "Termination was accepted from yourself"
    assert (
        terminate_accepted_text("self", "  PR ready  ")
        == "Termination was accepted from yourself: PR ready"
    )
    assert (
        terminate_accepted_text("agent:405", "done")
        == "Termination was accepted from agent:405: done"
    )
