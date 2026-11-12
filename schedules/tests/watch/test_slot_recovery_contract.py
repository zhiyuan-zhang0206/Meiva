from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from schedules.jobs.watch.main_red_slot import slot_round
from schedules.jobs.watch.main_red_watch import SlotLedger, WatchState, load_state, save_state
from schedules.jobs.watch.release_gate_reads import read_scheduled_starts
from schedules.plans.watch.release_gate_slot import SlotFacts
from schedules.plans.watch.main_red import Entry

NOW = datetime(2026, 11, 12, 6, 10, tzinfo=UTC)
OLD = datetime(2026, 11, 11, 19, 39, 12, tzinfo=UTC)


def seed(path: Path, gap: str | None = OLD.isoformat()) -> None:
    save_state(
        path,
        WatchState(
            {
                "ci.yml::other": Entry(
                    "ci.yml::other", "confirmed", "old", OLD, "old", OLD, 2, task_id=9000
                )
            },
            {"refresh-test-durations.yml": "independent"},
            {},
            slot=SlotLedger(gap_id=gap, task_id=6817),
            greens={"ci.yml": "retained"},
        ),
    )


def invoke(path: Path, facts: SlotFacts | None) -> tuple[list[object], list[tuple[int, str]]]:
    opened: list[object] = []
    closed: list[tuple[int, str]] = []

    def open_card(card: object) -> int:
        opened.append(card)
        return 9999

    _ = slot_round(
        state_path=path,
        now=NOW,
        facts=facts,
        open_card=open_card,
        close_card=lambda identifier, reason: closed.append((identifier, reason)),
        unreadable="unverified scheduled history",
    )
    return opened, closed


@pytest.mark.parametrize(
    "facts",
    [
        SlotFacts(OLD, True, 3),
        SlotFacts(OLD, False, 0),
        SlotFacts(None, False, 3),
        SlotFacts(OLD - timedelta(hours=1), False, 3),
        None,
    ],
)
def test_an_existing_gap_waits_without_new_scheduled_evidence(
    tmp_path: Path, facts: SlotFacts | None
) -> None:
    path = tmp_path / "state.json"
    seed(path)
    opened, closed = invoke(path, facts)
    state = load_state(path)
    assert opened == closed == []
    assert state.slot.task_id == 6817 and state.slot.gap_id == OLD.isoformat()
    assert state.greens == {"ci.yml": "retained"}
    assert state.watermarks == {"refresh-test-durations.yml": "independent"}


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [
        ("completed", "success"),
        ("completed", "failure"),
        ("queued", None),
        ("in_progress", None),
    ],
)
def test_a_verified_arrival_resolves_independently_of_its_result(
    tmp_path: Path, status: str, conclusion: str | None
) -> None:
    raw = {
        "id": 43158699999,
        "workflow_id": 400219585,
        "event": "schedule",
        "head_branch": "main",
        "path": ".github/workflows/release-gate.yml",
        "created_at": (NOW - timedelta(minutes=1)).isoformat(),
        "status": status,
        "conclusion": conclusion,
    }

    def read(_path: str) -> str:
        return json.dumps({"total_count": 1, "workflow_runs": [raw]})

    starts = read_scheduled_starts("owner/repo", "release-gate.yml", branch="main", read=read)
    path = tmp_path / "state.json"
    seed(path)
    opened, closed = invoke(
        path, SlotFacts(starts[0], status in ("queued", "in_progress"), 3, scheduled_starts=starts)
    )
    assert opened == []
    assert [identifier for identifier, _ in closed] == [6817]
    assert "scheduled" in closed[0][1]
    state = load_state(path)
    assert state.slot.task_id is None
    assert state.entries["ci.yml::other"].task_id == 9000
    assert state.watermarks == {"refresh-test-durations.yml": "independent"}


@pytest.mark.parametrize("gap", [None, "", "unparseable", "2026-11-11T19:39:12"])
def test_unverifiable_legacy_gap_identity_does_not_resolve(tmp_path: Path, gap: str | None) -> None:
    path = tmp_path / "state.json"
    seed(path, gap)
    opened, closed = invoke(path, SlotFacts(NOW, False, 3))
    assert opened == closed == []
    assert load_state(path).slot.task_id == 6817


def test_a_recorded_no_run_gap_ends_on_its_first_verified_arrival(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    seed(path, "no-run-on-record")
    _, closed = invoke(path, SlotFacts(OLD, True, 0))
    assert [identifier for identifier, _ in closed] == [6817]


@pytest.mark.parametrize("facts", [SlotFacts(OLD, True, 3), SlotFacts(OLD, False, 0)])
def test_suppression_without_an_existing_gap_opens_nothing(
    tmp_path: Path, facts: SlotFacts
) -> None:
    path = tmp_path / "state.json"
    assert invoke(path, facts) == ([], [])
    assert load_state(path).slot.task_id is None


def test_a_new_arrival_already_stale_names_a_distinct_next_gap(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    seed(path)
    facts = SlotFacts(OLD + timedelta(seconds=1), False, 3)
    assert [identifier for identifier, _ in invoke(path, facts)[1]] == [6817]
    opened, closed = invoke(path, facts)
    assert len(opened) == 1 and closed == []
    assert facts.last_schedule_start is not None
    assert load_state(path).slot.gap_id == facts.last_schedule_start.isoformat()
    assert invoke(path, facts) == ([], [])


def test_retry_started_at_does_not_advance_a_scheduled_arrival(tmp_path: Path) -> None:
    raw = {
        "id": 43089191621,
        "workflow_id": 400219585,
        "event": "schedule",
        "head_branch": "main",
        "path": ".github/workflows/release-gate.yml",
        "created_at": OLD.isoformat(),
        "run_started_at": NOW.isoformat(),
        "run_attempt": 2,
        "status": "in_progress",
    }

    def read(_path: str) -> str:
        return json.dumps({"total_count": 1, "workflow_runs": [raw]})

    starts = read_scheduled_starts("owner/repo", "release-gate.yml", branch="main", read=read)
    path = tmp_path / "state.json"
    seed(path)
    assert starts == (OLD,)
    assert invoke(path, SlotFacts(starts[0], True, 3, scheduled_starts=starts)) == ([], [])
    assert load_state(path).slot.task_id == 6817
