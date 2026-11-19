from __future__ import annotations

from base.agents.messages.lifecycle_text import (
    fast_acknowledgement_note,
    restart_accepted_text,
    restart_resume_note,
)


def test_restart_accepted_text_formats_source_and_optional_explanation() -> None:
    assert restart_accepted_text("user") == "Restart was accepted from user"
    assert restart_accepted_text("self") == "Restart was accepted from yourself"
    assert restart_accepted_text("agent:7") == "Restart was accepted from agent:7"
    assert restart_accepted_text("system:restart") == "Restart was accepted from system:restart"
    assert (
        restart_accepted_text("user", "Zhiyuan switching model to gpt-5.6-sol")
        == "Restart was accepted from user: Zhiyuan switching model to gpt-5.6-sol"
    )
    assert restart_accepted_text("user", None) == "Restart was accepted from user"
    assert restart_accepted_text("user", "") == "Restart was accepted from user"


def test_restart_resume_note_carries_optional_explanation() -> None:
    bare = restart_resume_note("system:restart")
    assert bare.startswith(
        "Restart was accepted from system:restart while your turn was unfinished. If code was running"
    )
    assert "Zhiyuan" not in bare

    explained = restart_resume_note("system:restart", "Zhiyuan rolling out #9999")
    assert explained.startswith(
        "Restart was accepted from system:restart while your turn was unfinished: "
        "Zhiyuan rolling out #9999\nIf code was running"
    )


def test_lifecycle_text_carries_fast_acknowledgement_updates() -> None:
    on_note = fast_acknowledgement_note({"fast_acknowledgement": True})
    off_note = fast_acknowledgement_note({"config_overlay": {"fast_acknowledgement": False}})
    assert on_note is not None and "[Config updated: fast_acknowledgement=on]" in on_note
    assert off_note is not None and "[Config updated: fast_acknowledgement=off]" in off_note
    assert fast_acknowledgement_note({"fast_acknowledgement": "true"}) is None
    assert fast_acknowledgement_note(None) is None
