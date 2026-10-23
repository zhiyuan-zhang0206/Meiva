from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, get_type_hints

import pytest
from pydantic import ValidationError

from agent.hooks.compact_events import (
    compact_finished_event,
    compact_started_event,
    emit_compact_finished,
    emit_compact_started,
)
from base.events.live.projection import (
    EVENT_ADAPTER,
    CompactFinished,
    CompactionMode,
    CompactionStatus,
    CompactStarted,
)


def test_producer_and_projection_annotations_have_one_owner_per_dimension() -> None:
    assert get_type_hints(emit_compact_started)["mode"] is CompactionMode
    assert CompactStarted.model_fields["mode"].annotation is CompactionMode
    assert get_type_hints(emit_compact_finished)["status"] is CompactionStatus
    assert CompactFinished.model_fields["status"].annotation is CompactionStatus
    assert {member.value for member in CompactionMode} == {"auto", "request"}
    assert {member.value for member in CompactionStatus} == {"success", "failure", "replaced"}


_STARTED_AT = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
_FINISHED_AT = datetime(2026, 10, 7, 12, 0, 9, tzinfo=UTC)


@pytest.mark.parametrize("mode", list(CompactionMode))
@pytest.mark.parametrize("status", list(CompactionStatus))
def test_each_compaction_pair_serializes_and_restores_existing_wire_values(
    mode: CompactionMode,
    status: CompactionStatus,
) -> None:
    started_raw = compact_started_event(7, compact_id="run1", started_at=_STARTED_AT, mode=mode)
    finished_raw = compact_finished_event(7, "run1", finished_at=_FINISHED_AT, status=status)
    assert finished_raw is not None
    started = EVENT_ADAPTER.validate_json(started_raw)
    finished = EVENT_ADAPTER.validate_json(finished_raw)
    assert isinstance(started, CompactStarted) and started.mode is mode
    assert isinstance(finished, CompactFinished) and finished.status is status
    assert json.loads(started_raw)["mode"] == mode.value
    assert json.loads(finished_raw)["status"] == status.value
    assert json.loads(started_raw)["started_at"] == _STARTED_AT.isoformat()
    assert json.loads(finished_raw)["finished_at"] == _FINISHED_AT.isoformat()
    assert started.compact_id == finished.compact_id == "run1"


@pytest.mark.parametrize("invalid", [None, "unknown", "", 3, {}])
@pytest.mark.parametrize(
    "role,field", [("compact_started", "mode"), ("compact_finished", "status")]
)
def test_raw_event_domain_rejects_unknown_values(role: str, field: str, invalid: Any) -> None:
    raw = {
        "agent_id": 7,
        "role": role,
        "compact_id": "run1",
        "started_at": "now",
        "finished_at": "later",
        field: invalid,
    }
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(raw)


def test_absent_publisher_does_not_start_a_live_run() -> None:
    assert emit_compact_started(None, 7, mode=CompactionMode.REQUEST) is None


def test_a_finish_without_a_run_id_has_nothing_to_close() -> None:
    finished = compact_finished_event(
        7, None, finished_at=_FINISHED_AT, status=CompactionStatus.REPLACED
    )

    assert finished is None
