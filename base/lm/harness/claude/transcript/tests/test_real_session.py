# meiva:note
# A sample of one long real interactive session (`fixtures/long_session`, sanitized to structure; see its
# `source.json`), for two things the minimal hand-made rows could not show: the bookkeeping rows of a newer CLI (titles,
# links, modes, bridge and request-shape rows) are ignored on purpose and are not reported as unknown, and the origin
# the CLI puts on queued commands and user rows is read as it is: `human` is an input like any other (it is the id we gave
# it that settles it), `task-notification` is a notice, and an origin the vocabulary does not know (`peer`) is reported
# by the projection and puts nothing on the timeline (the parser only keeps what the row says).
# meiva:link base/lm/harness/docs/transcript-projection.md

from __future__ import annotations

from base.lm.harness.claude.transcript.parse import ParseState, parse_row
from base.lm.harness.claude.transcript.tests.support import entries_of
from base.lm.harness.events import HarnessCause
from base.lm.harness.session.transcript.entries import (
    Anomaly,
    AnomalyKind,
    HumanInput,
    ToolCall,
    ToolResult,
    TurnEnd,
)
from base.lm.harness.session.transcript.projection import Projector


def test_the_bookkeeping_rows_of_a_long_session_are_ignored_not_reported_as_unknown() -> None:
    entries = entries_of("long_session")
    assert not [e for e in entries if isinstance(e, Anomaly)], (
        "the parser reports nothing of this session"
    )
    reported = Projector().feed(entries).anomalies
    assert [(a.anomaly, a.detail) for a in reported] == [(AnomalyKind.UNKNOWN_TYPE, "origin/peer")]


def test_a_real_turn_is_one_human_input_its_calls_with_results_and_one_end() -> None:
    entries = entries_of("long_session")
    turn = [e for e in entries if not isinstance(e, Anomaly)]
    humans = [e for e in turn if isinstance(e, HumanInput) and e.origin is None]
    assert [(h.joined_running_turn, h.wire_id is not None) for h in humans] == [
        (False, True),
        (False, True),
        (True, True),
    ], "two prompts, and the queued command as a steer that carries the id the CLI kept"
    calls = [e for e in turn if isinstance(e, ToolCall)]
    results = [e for e in turn if isinstance(e, ToolResult)]
    assert calls and {r.call_id for r in results} <= {c.call_id for c in calls}
    assert sum(isinstance(e, TurnEnd) for e in turn) >= 1


def test_a_task_notification_is_a_notice_with_its_cause() -> None:
    told = [e for e in entries_of("long_session") if isinstance(e, HumanInput) and e.origin]
    notices = [
        e.origin for e in told if e.origin and e.origin.cause is HarnessCause.BACKGROUND_TASK
    ]
    assert len(notices) == 1 and notices[0] is not None
    assert notices[0].text.startswith("Background task ")
    shown = Projector().feed(entries_of("long_session")).drafts
    assert [d.payload for d in shown if d.source == HarnessCause.BACKGROUND_TASK.value] == [
        notices[0].text
    ]


# meiva:note
# Two rows production records hold that no recording of ours does (found by a statistic over the records of real
# sessions that counted row kinds, key names and value types and read no content; the values here are invented): the
# notice of a request that failed and is retried, and the attachment that tells the model its permission mode. Neither
# is something a timeline shows, and neither is reported as unknown.
def test_a_retry_notice_and_a_mode_attachment_are_ignored_not_reported_as_unknown() -> None:
    common = {
        "uuid": "11111111-1111-4111-8111-111111111111",
        "timestamp": "2026-11-01T00:00:00.000Z",
        "sessionId": "s",
        "isSidechain": False,
    }
    retry = {
        **common,
        "type": "system",
        "subtype": "api_error",
        "level": "error",
        "source": "request_retry",
        "retryAttempt": 1,
        "maxRetries": 10,
        "retryInMs": 500,
        "error": {"message": "x", "formatted": "x", "isNetworkDown": False},
    }
    mode = {
        **common,
        "type": "attachment",
        "attachment": {"type": "auto_mode", "bypass": True, "steerOnly": True},
        "renderedRole": "system",
    }
    for row in (retry, mode):
        _state, entries = parse_row(ParseState(), row, line=0)
        assert entries == ()
