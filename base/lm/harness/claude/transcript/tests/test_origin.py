# meiva:note
# A queued command that names an origin is read as a notification only when the origin is one the CLI is known to use
# for what it tells the conversation itself (a finished background task, a timer). An origin this parser does not know
# is an anomaly and nothing else: it is not turned into a notice, so that an origin the CLI may one day put on the
# inputs we send ourselves can never make them look like notices that settle nothing.
# meiva:link base/lm/harness/docs/transcript-projection.md
from __future__ import annotations

from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.session.transcript.entries import Anomaly, AnomalyKind, Notice


def _queued(origin_kind: str) -> dict[str, object]:
    return {
        "type": "attachment",
        "uuid": "u-1",
        "timestamp": "2026-11-12T10:00:00.000Z",
        "attachment": {
            "type": "queued_command",
            "source_uuid": "s-1",
            "prompt": "<task-notification><task-id>b1</task-id><status>completed</status></task-notification>",
            "origin": {"kind": origin_kind},
        },
    }


def test_a_known_origin_is_a_notification_and_an_unknown_one_is_an_anomaly() -> None:
    _, (known,) = parse_row(ParseState(), _queued("task-notification"), line=3)
    assert isinstance(known, Notice) and known.text.startswith("Background task b1")
    _, (unknown,) = parse_row(ParseState(), _queued("something-new"), line=4)
    assert isinstance(unknown, Anomaly)
    assert (unknown.anomaly, unknown.detail) == (AnomalyKind.UNKNOWN_TYPE, "origin/something-new")
