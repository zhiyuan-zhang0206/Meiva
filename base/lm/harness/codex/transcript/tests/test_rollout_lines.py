from __future__ import annotations

from base.lm.harness.codex.transcript.parse import parse_rollout, parse_rollout_line
from base.lm.harness.codex.transcript.tests.support import as_obj, load_lines, project
from base.lm.harness.session.transcript.entries import (
    MAIN_RECORD,
    Anomaly,
    AnomalyKind,
    Interrupt,
    TurnEnd,
)

TURN = "01a1cefa-641e-7311-b99e-8c19287d1fa1"
STAMP = "2026-11-12T08:57:13.042Z"
QUIET_LINES = {"session_meta", "turn_context", "world_state"}
QUIET_PAYLOADS = {
    "task_started",
    "token_count",
    "thread_settings_applied",
    "custom_tool_call",
    "custom_tool_call_output",
}


def test_the_bookkeeping_rows_of_a_real_rollout_give_no_entries_and_no_anomalies() -> None:
    lines = load_lines("subagent/rollout.jsonl")
    quiet = [
        line
        for line in lines
        if line["type"] in QUIET_LINES or as_obj(line["payload"]).get("type") in QUIET_PAYLOADS
    ]
    assert len(quiet) > 10
    for line in quiet:
        assert parse_rollout_line(line, record=MAIN_RECORD) == []
    assert not [e for e in parse_rollout(lines, record=MAIN_RECORD) if isinstance(e, Anomaly)]


def _line(ordinal: int, kind: str, payload: dict[str, object]) -> dict[str, object]:
    return {"type": kind, "timestamp": STAMP, "ordinal": ordinal, "payload": payload}


def test_a_row_of_a_kind_the_parser_does_not_know_is_an_anomaly_not_an_error() -> None:
    cases: list[tuple[object, AnomalyKind, str]] = [
        ("not an object", AnomalyKind.MISSING_FIELD, "type"),
        (_line(1, "future_row", {}), AnomalyKind.UNKNOWN_TYPE, "future_row"),
        (
            _line(2, "event_msg", {"type": "future_event"}),
            AnomalyKind.UNKNOWN_TYPE,
            "event_msg/future_event",
        ),
        (
            _line(3, "response_item", {"type": "web_search_call"}),
            AnomalyKind.UNKNOWN_TYPE,
            "response_item/web_search_call",
        ),
        (
            _line(
                4,
                "event_msg",
                {
                    "type": "item_completed",
                    "turn_id": TURN,
                    "started_at_ms": 1,
                    "item": {"type": "FileChange", "id": "fc-1"},
                },
            ),
            AnomalyKind.UNKNOWN_TYPE,
            "item_completed/FileChange",
        ),
    ]
    for line, kind, detail in cases:
        (entry,) = parse_rollout_line(line, record=MAIN_RECORD)
        assert isinstance(entry, Anomaly)
        assert (entry.anomaly, entry.detail) == (kind, detail)


def test_a_row_without_its_position_is_a_missing_field() -> None:
    line = _line(0, "event_msg", {"type": "task_complete", "turn_id": TURN})
    del line["ordinal"]
    (entry,) = parse_rollout_line(line, record=MAIN_RECORD)
    assert isinstance(entry, Anomaly)
    assert (entry.anomaly, entry.detail) == (AnomalyKind.MISSING_FIELD, "ordinal")


# meiva:note
# No recording holds a `turn_aborted` row (scenario s3 was recorded without its rollout), so this line
# has the shape the research report gives (`event_msg/turn_aborted` with `reason`, `started_at`,
# `completed_at`, `duration_ms`) plus the `turn_id` every other turn row carries; it is not real data.
def test_a_turn_aborted_event_is_an_interrupt_that_closes_its_turn() -> None:
    line = _line(
        20, "event_msg", {"type": "turn_aborted", "turn_id": TURN, "reason": "interrupted"}
    )
    (entry,) = parse_rollout_line(line, record=MAIN_RECORD)
    assert isinstance(entry, Interrupt) and entry.turn == TURN
    _, projection = project([entry])
    assert [(closed.turn, closed.reason) for closed in projection.turns_closed] == [
        (TURN, "interrupted")
    ]
    done = _line(21, "event_msg", {"type": "task_complete", "turn_id": TURN})
    (end,) = parse_rollout_line(done, record=MAIN_RECORD)
    assert isinstance(end, TurnEnd) and end.key == f"turn-end:{TURN}"
