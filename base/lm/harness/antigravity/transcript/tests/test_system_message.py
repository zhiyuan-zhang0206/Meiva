from __future__ import annotations

import pytest

from base.lm.harness.antigravity.transcript.errors import UnrecognisedError
from base.lm.harness.antigravity.transcript.system_message import classify
from base.lm.harness.events import HarnessCause


@pytest.mark.parametrize(
    "content",
    [
        (
            "The following is a <SYSTEM_MESSAGE> not actually sent by the user. It is provided by "
            "the system as important information to pay attention to.\n\n"
            "<SYSTEM_MESSAGE>\n"
            "Reminder: work efficiently. In this turn, issue in parallel every independent search, "
            "read and edit or write you need next.\n"
            "</SYSTEM_MESSAGE>"
        ),
        "<SYSTEM_MESSAGE>\nReminder: keep tool calls batched.\n</SYSTEM_MESSAGE>",
        "[Notice] All background commands have been stopped due to server restart.",
        (
            "<SYSTEM_MESSAGE>\n"
            "[Message] timestamp=2026-11-22T00:00:00Z sender=system priority=normal content=ignored\n"
            "</SYSTEM_MESSAGE>"
        ),
    ],
)
def test_known_cli_system_noise_is_ignored(content: str) -> None:
    assert classify(content) is None


def test_message_envelope_wins_over_quoted_reminder_text() -> None:
    content = (
        "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n"
        "<SYSTEM_MESSAGE>\n"
        "[Message] timestamp=2026-11-22T00:00:00Z sender=child-conv priority=HIGH "
        "content=Saw <SYSTEM_MESSAGE>Reminder: work efficiently</SYSTEM_MESSAGE> in output\n"
        "</SYSTEM_MESSAGE>"
    )
    notice = classify(content)
    assert notice is not None
    assert notice.sender == "child-conv"
    assert notice.cause is HarnessCause.BACKGROUND_TASK
    assert "Saw" in notice.text


@pytest.mark.parametrize(
    "content",
    [
        "DO_NOT_LOG_SYNTHETIC_BODY",
        "<SYSTEM_MESSAGE>\nUnknown CLI frame without envelope\n</SYSTEM_MESSAGE>",
        "<SYSTEM_MESSAGE>\nReminder:\n</SYSTEM_MESSAGE>",
    ],
)
def test_unknown_system_messages_still_raise(content: str) -> None:
    with pytest.raises(UnrecognisedError):
        classify(content)
