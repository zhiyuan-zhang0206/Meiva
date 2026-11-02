# meiva:note
# The trunk-red watch's effect shell, driven by a stand-in for `gh` and a recording stand-in for
# the task API: what must hold is that one confirmed red opens exactly one card, a later green run
# closes it, and a round always leaves a heartbeat.
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from schedules.jobs.watch.trunk_red_watch import STATE_FILE, load_state, watch_once
from schedules.plans.watch.trunk_red import FAILURE_TEST, Card, RunFacts


def _red(
    workflow: str, sha: str, when: datetime, test_id: str = "pkg/tests/test_a.py::test_x"
) -> RunFacts:
    from schedules.plans.watch.trunk_red import Failure

    return RunFacts(workflow, sha, when, "failure", (Failure(test_id, FAILURE_TEST),))


def _green(workflow: str, sha: str, when: datetime) -> RunFacts:
    return RunFacts(workflow, sha, when, "success", ())


def _watch(
    tmp_path: Path,
    runs: Sequence[RunFacts],
    *,
    opened: list[Card],
    closed: list[tuple[int, str]],
    heartbeats: list[dict[str, object]],
    now: datetime | None = None,
) -> str:
    def fetch(_workflow: str) -> tuple[RunFacts, ...]:
        return tuple(runs)

    def open_card(card: Card) -> int:
        opened.append(card)
        return 899 + len(opened)

    def close_card(task_id: int, reason: str) -> None:
        closed.append((task_id, reason))

    def emit(payload: Mapping[str, object]) -> None:
        heartbeats.append(dict(payload))

    return watch_once(
        state_path=tmp_path / STATE_FILE,
        now=now or datetime(2026, 11, 2, 12, tzinfo=UTC),
        fetch=fetch,
        open_card=open_card,
        close_card=close_card,
        emit=emit,
        workflows=("release-gate.yml",),
        release_gate_last_start=(now or datetime(2026, 11, 2, 12, tzinfo=UTC)) - timedelta(hours=1),
        nightly_last_start=(now or datetime(2026, 11, 2, 12, tzinfo=UTC)) - timedelta(hours=2),
    )


def test_a_first_red_opens_no_card_and_still_leaves_a_heartbeat(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []

    line = _watch(
        tmp_path,
        [_red("release-gate.yml", "sha1", datetime(2026, 11, 2, 6, tzinfo=UTC))],
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )

    assert opened == []
    assert heartbeats and heartbeats[0]["confirmed"] == 0
    assert "confirmed=0" in line
    entries, _ = load_state(tmp_path / STATE_FILE)
    assert len(entries) == 1


def test_the_second_red_on_another_commit_opens_one_card_and_the_third_opens_none(
    tmp_path: Path,
) -> None:
    opened: list[Card] = []
    _watch(
        tmp_path,
        [
            _red("release-gate.yml", "sha1", datetime(2026, 11, 1, 6, tzinfo=UTC)),
            _red("release-gate.yml", "sha2", datetime(2026, 11, 1, 12, tzinfo=UTC)),
        ],
        opened=opened,
        closed=[],
        heartbeats=[],
    )
    _watch(
        tmp_path,
        [
            _red("release-gate.yml", "sha3", datetime(2026, 11, 1, 18, tzinfo=UTC)),
        ],
        opened=opened,
        closed=[],
        heartbeats=[],
    )

    assert len(opened) == 1
    entries, _ = load_state(tmp_path / STATE_FILE)
    (entry,) = entries.values()
    assert entry.red_count == 3
    assert entry.task_id == 900


def test_a_green_run_closes_the_card(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    _watch(
        tmp_path,
        [
            _red("release-gate.yml", "sha1", datetime(2026, 11, 1, 6, tzinfo=UTC)),
            _red("release-gate.yml", "sha2", datetime(2026, 11, 1, 12, tzinfo=UTC)),
        ],
        opened=opened,
        closed=closed,
        heartbeats=[],
    )
    _watch(
        tmp_path,
        [_green("refresh-test-durations.yml", "sha3", datetime(2026, 11, 1, 23, tzinfo=UTC))],
        opened=opened,
        closed=closed,
        heartbeats=[],
    )

    assert closed and closed[0][0] == 900
    entries, _ = load_state(tmp_path / STATE_FILE)
    assert entries == {}


def test_a_stale_schedule_is_reported_in_the_round_line(tmp_path: Path) -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    line = _watch(
        tmp_path,
        [],
        opened=[],
        closed=[],
        heartbeats=[],
        now=now,
    )
    late = watch_once(
        state_path=tmp_path / STATE_FILE,
        now=now,
        fetch=lambda _w: (),
        open_card=lambda _c: 1,
        close_card=lambda _t, _r: None,
        emit=lambda _p: None,
        workflows=("release-gate.yml",),
        release_gate_last_start=now - timedelta(hours=12),
        nightly_last_start=now - timedelta(hours=2),
    )

    assert "no new run" not in line
    assert "no new run" in late
