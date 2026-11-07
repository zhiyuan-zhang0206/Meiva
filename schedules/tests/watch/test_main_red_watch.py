# meiva:note
# The main-red watch's effect shell, driven by a stand-in for `gh` and a recording stand-in for
# the task API: what must hold is that one confirmed red opens exactly one card, a later green run
# closes it, and a round always leaves a heartbeat.
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from base.db import Database
from schedules.jobs.watch.main_red_liveness import liveness_once
from schedules.jobs.watch.main_red_watch import (
    LivenessStream,
    card_operation_key,
    STATE_FILE,
    WatchState,
    load_state,
    save_state,
    slot_round,
    watch_once,
)
from schedules.plans.watch.main_red import (
    FAILURE_TEST,
    FAILURE_UNNAMED,
    NIGHTLY_STALE_AFTER,
    Card,
    Failure,
    RunFacts,
)
from schedules.plans.watch.release_gate_slot import RELEASE_GATE_SLOT_CADENCE, SlotFacts

# meiva:note
# The three streams whose silence is reported, as the schedule entry wires them: the helper below
# drives the same table so a test cannot pass against a shape production does not use.
_LIVENESS = (
    LivenessStream("nightly", "refresh-test-durations.yml", NIGHTLY_STALE_AFTER),
    LivenessStream("nightly-full", "ci.yml", NIGHTLY_STALE_AFTER),
)


def _red(
    workflow: str,
    sha: str,
    when: datetime,
    test_id: str = "pkg/tests/test_a.py::test_x",
    *,
    full_suite: bool = True,
    partial_job: bool = False,
    title: str = "",
) -> RunFacts:
    from schedules.plans.watch.main_red import Failure

    return RunFacts(
        workflow,
        sha,
        when,
        "failure",
        (Failure(test_id, FAILURE_TEST, covers_full_suite=not partial_job),),
        covers_full_suite=full_suite,
        head_title=title,
    )


def _green(workflow: str, sha: str, when: datetime, *, full_suite: bool = True) -> RunFacts:
    return RunFacts(workflow, sha, when, "success", (), covers_full_suite=full_suite)


def _watch(
    tmp_path: Path,
    runs: Sequence[RunFacts],
    *,
    opened: list[Card],
    closed: list[tuple[int, str]],
    heartbeats: list[dict[str, object]],
    now: datetime | None = None,
    workflows: tuple[str, ...] = ("release-gate.yml",),
    starts: Mapping[str, datetime | None] | None = None,
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

    clock = now or datetime(2026, 11, 2, 12, tzinfo=UTC)

    def last_start(workflow: str) -> datetime | None:
        if starts is not None:
            return starts[workflow]
        return clock - timedelta(hours=1)

    return watch_once(
        state_path=tmp_path / STATE_FILE,
        now=clock,
        fetch=fetch,
        last_start=last_start,
        open_card=open_card,
        close_card=close_card,
        emit=emit,
        workflows=workflows,
        liveness=_LIVENESS,
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
    entries = load_state(tmp_path / STATE_FILE).entries
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
    entries = load_state(tmp_path / STATE_FILE).entries
    (entry,) = entries.values()
    assert entry.red_count == 3
    assert entry.task_id == 900


# meiva:note
# The macOS job rides `ci.yml`'s nightly verdict, so the card it opens arrives in the same workflow
# whose Linux green would otherwise retire it - a run that never collected the test is not repair.
# The state file remembers the kind of red across rounds (this test reloads it three times) and a
# later `ci.yml` green retires it, because that is the run that collects the test.
def test_a_partial_jobs_card_survives_the_populations_green_and_closes_on_its_own(
    tmp_path: Path,
) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    lane = "ci.yml"
    _watch(
        tmp_path,
        [
            _red(
                lane,
                "sha1",
                datetime(2026, 11, 8, 21, 47, tzinfo=UTC),
                partial_job=True,
            ),
            _red(
                lane,
                "sha2",
                datetime(2026, 11, 9, 21, 47, tzinfo=UTC),
                partial_job=True,
            ),
        ],
        opened=opened,
        closed=closed,
        heartbeats=[],
        workflows=(lane,),
    )
    assert len(opened) == 1
    (entry,) = load_state(tmp_path / STATE_FILE).entries.values()
    assert entry.covers_full_suite is False

    _watch(
        tmp_path,
        [_green("refresh-test-durations.yml", "sha3", datetime(2026, 11, 10, 2, tzinfo=UTC))],
        opened=opened,
        closed=closed,
        heartbeats=[],
        workflows=(lane,),
    )
    assert closed == []
    assert load_state(tmp_path / STATE_FILE).entries != {}

    _watch(
        tmp_path,
        [_green(lane, "sha4", datetime(2026, 11, 10, 21, 47, tzinfo=UTC))],
        opened=opened,
        closed=closed,
        heartbeats=[],
        workflows=(lane,),
    )
    assert closed and closed[0][0] == 900
    assert load_state(tmp_path / STATE_FILE).entries == {}


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
    entries = load_state(tmp_path / STATE_FILE).entries
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

    def last_start(workflow: str) -> datetime | None:
        del workflow
        return now - timedelta(hours=30)

    late = watch_once(
        state_path=tmp_path / STATE_FILE,
        now=now,
        fetch=lambda _w: (),
        last_start=last_start,
        open_card=lambda _c: 1,
        close_card=lambda _t, _r: None,
        emit=lambda _p: None,
        workflows=("release-gate.yml",),
        liveness=_LIVENESS,
    )

    assert "no new run" not in line
    assert "no new run" in late


# meiva:note
# The watch's own liveness. A round that stops arriving is indistinguishable from a quiet `main`
# unless something opens a card, so the effect is driven here through the same injected opener the
# round tests use: no database, no clock, no task API.
class _Cursor:
    def __init__(self, value: datetime | None) -> None:
        self._value = value

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def execute(self, sql: str, params: object) -> None:
        del sql, params

    def fetchone(self) -> tuple[datetime | None]:
        return (self._value,)


class _Connection:
    def __init__(self, value: datetime | None) -> None:
        self._value = value

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def cursor(self) -> _Cursor:
        return _Cursor(self._value)


class _Database:
    def __init__(self, value: datetime | None) -> None:
        self._value = value

    def connect(self, *, autocommit: bool = False) -> _Connection:
        assert autocommit, "the liveness read opens no transaction it must commit"
        return _Connection(self._value)


def test_a_round_that_stopped_opens_one_card() -> None:
    now = datetime(2026, 11, 3, 10, 35, tzinfo=UTC)
    opened: list[Card] = []

    liveness_once(
        now,
        database=cast("Database", _Database(now - timedelta(minutes=61))),
        open_card=lambda card: opened.append(card) or 900,
        close_card=lambda: None,
    )

    assert [card.fingerprint for card in opened] == ["main-red-watch/liveness"]
    assert "stopped" in opened[0].title.lower()


def test_a_round_inside_the_hour_opens_nothing() -> None:
    now = datetime(2026, 11, 3, 10, 35, tzinfo=UTC)
    opened: list[Card] = []

    liveness_once(
        now,
        database=cast("Database", _Database(now - timedelta(minutes=31))),
        open_card=lambda card: opened.append(card) or 900,
        close_card=lambda: None,
    )

    assert opened == []


# meiva:note
# The card promises "closes when a round lands again"; this is the test that keeps that sentence
# true. Remove the `close_card()` call in `liveness_once` and it goes red.
def test_a_round_that_lands_again_closes_the_card() -> None:
    now = datetime(2026, 11, 3, 10, 40, tzinfo=UTC)
    closed: list[str] = []

    liveness_once(
        now,
        database=cast("Database", _Database(now - timedelta(minutes=2))),
        open_card=lambda card: 900,
        close_card=lambda: closed.append("closed"),
    )

    assert closed == ["closed"]


def test_a_watch_still_silent_opens_without_closing() -> None:
    now = datetime(2026, 11, 3, 10, 35, tzinfo=UTC)
    closed: list[str] = []

    liveness_once(
        now,
        database=cast("Database", _Database(now - timedelta(minutes=61))),
        open_card=lambda card: 900,
        close_card=lambda: closed.append("closed"),
    )

    assert closed == []


# meiva:note
# The same invariant on the other card the watch opens by itself: a watch that stays stopped must
# produce one card, so the card may not report how long it has been stopped. Remove the fixed
# `episode` and this goes red with two different requests under one key.
def test_the_liveness_card_reads_the_same_while_the_watch_stays_stopped() -> None:
    last = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    opened: list[Card] = []

    for slot in (last + timedelta(minutes=61), last + timedelta(hours=5)):
        liveness_once(
            slot,
            database=cast("Database", _Database(last)),
            open_card=lambda card: opened.append(card) or 900,
            close_card=lambda: None,
        )

    assert len(opened) == 2
    assert opened[0] == opened[1]


# meiva:note
# A schedule that never ran is the manager's problem, not this card's: a fresh site must not open a
# liveness card the first time the check runs.
def test_a_watch_with_no_round_on_record_opens_nothing() -> None:
    opened: list[Card] = []

    liveness_once(
        datetime(2026, 11, 3, 10, 35, tzinfo=UTC),
        database=cast("Database", _Database(None)),
        open_card=lambda card: opened.append(card) or 900,
        close_card=lambda: None,
    )

    assert opened == []


# meiva:note
# The nightly full run of `ci.yml` is its own stream, not a shade of the duration refresh: a red in
# either reaches the lead, and either schedule going quiet is said out loud. Without the second half
# a nightly that stopped being triggered reads exactly like a `main` that stayed green.
def test_a_red_nightly_full_run_opens_a_card_and_its_silence_is_reported(tmp_path: Path) -> None:
    opened: list[Card] = []
    first = datetime(2026, 11, 1, 18, tzinfo=UTC)
    second = datetime(2026, 11, 2, 18, tzinfo=UTC)

    _watch(
        tmp_path,
        [_red("ci.yml", "sha1", first)],
        opened=opened,
        closed=[],
        heartbeats=[],
        now=second,
    )
    assert opened == []

    line = _watch(
        tmp_path,
        [_red("ci.yml", "sha2", second)],
        opened=opened,
        closed=[],
        heartbeats=[],
        now=second,
    )
    assert len(opened) == 1
    assert "ci.yml" in opened[0].fingerprint
    assert "opened card 900" in line

    silent = watch_once(
        state_path=tmp_path / "other" / STATE_FILE,
        now=second,
        fetch=lambda _w: (),
        open_card=lambda _c: 1,
        close_card=lambda _t, _r: None,
        emit=lambda _p: None,
        last_start=lambda workflow: second - timedelta(hours=30) if workflow == "ci.yml" else None,
        workflows=(),
        liveness=(LivenessStream("nightly-full", "ci.yml", NIGHTLY_STALE_AFTER),),
    )

    assert "nightly-full" in silent
    assert "no new run" in silent


# meiva:note
# A card names the merge it points at: the sha alone sends a reader to `git log` before they can
# tell whether the change is theirs. The subject is read for a failing run only.
def test_a_card_carries_the_commit_subject_of_the_failing_run(tmp_path: Path) -> None:
    opened: list[Card] = []
    first = datetime(2026, 11, 1, 6, tzinfo=UTC)
    second = datetime(2026, 11, 1, 12, tzinfo=UTC)

    _watch(
        tmp_path,
        [_red("release-gate.yml", "sha1aaaaaaa", first, title="chore(x): first merge")],
        opened=opened,
        closed=[],
        heartbeats=[],
    )
    _watch(
        tmp_path,
        [_red("release-gate.yml", "sha2bbbbbbb", second, title="feat(y): second merge")],
        opened=opened,
        closed=[],
        heartbeats=[],
    )

    assert len(opened) == 1
    assert "sha2bbbbbbb" in opened[0].body
    assert "feat(y): second merge" in opened[0].body


# meiva:note
# The push gate runs two checks, so its green run says nothing about a test the nightly found: it
# must not close that card. This is the line most likely to be "simplified" into closing every
# green run, and that would silently drop a real red from the board.
def test_a_green_narrow_gate_does_not_close_a_card_the_nightly_opened(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    first = datetime(2026, 11, 1, 12, tzinfo=UTC)
    second = datetime(2026, 11, 1, 18, tzinfo=UTC)

    _watch(
        tmp_path,
        [
            _red("ci.yml", "sha1", first),
            _red("ci.yml", "sha2", second),
        ],
        opened=opened,
        closed=closed,
        heartbeats=[],
        workflows=("ci.yml",),
    )
    assert len(opened) == 1

    _watch(
        tmp_path,
        [_green("main-push.yml", "sha3", second, full_suite=False)],
        opened=opened,
        closed=closed,
        heartbeats=[],
        workflows=("main-push.yml",),
    )
    assert closed == []

    _watch(
        tmp_path,
        [_green("ci.yml", "sha4", second + timedelta(hours=1))],
        opened=opened,
        closed=closed,
        heartbeats=[],
        workflows=("ci.yml",),
    )
    assert closed and closed[0][0] == 900


# meiva:note
# A stream the watch has only just met has no run on record because its cron has not come round --
# the first night of a new `ci.yml` schedule is exactly that, and reporting it would be a card about
# a schedule that is on time. Silence is reported once one threshold has passed since the watch
# first saw the stream.
def test_a_stream_first_seen_now_is_not_reported_until_a_threshold_passes(tmp_path: Path) -> None:
    first = datetime(2026, 11, 3, 18, tzinfo=UTC)

    def round_at(now: datetime) -> str:
        return watch_once(
            state_path=tmp_path / STATE_FILE,
            now=now,
            fetch=lambda _w: (),
            last_start=lambda _w: None,
            open_card=lambda _c: 1,
            close_card=lambda _t, _r: None,
            emit=lambda _p: None,
            workflows=(),
            liveness=(LivenessStream("nightly-full", "ci.yml", NIGHTLY_STALE_AFTER),),
        )

    assert "never started" not in round_at(first)
    assert "never started" not in round_at(first + NIGHTLY_STALE_AFTER - timedelta(minutes=1))
    assert "never started" in round_at(first + NIGHTLY_STALE_AFTER + timedelta(minutes=1))
    assert "no new run" not in round_at(first + timedelta(days=3))


# meiva:note
# One unreadable stream is a note, never the end of the round: the failure this was written for was
# a single `gh api .../jobs` 502 that took the other three streams down with it and stopped the
# process, so nothing reported the outage for twelve hours.
def test_one_unreadable_stream_leaves_the_others_read_and_the_round_whole(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []
    asked: list[str] = []

    def fetch(workflow: str) -> tuple[RunFacts, ...]:
        asked.append(workflow)
        if workflow == "main-push.yml":
            raise RuntimeError(
                "gh api repos/o/r/actions/runs/1/jobs failed: gh: Server Error (HTTP 502)"
            )
        return (_red("release-gate.yml", "sha1", datetime(2026, 11, 1, 6, tzinfo=UTC)),)

    line = watch_once(
        state_path=tmp_path / STATE_FILE,
        now=datetime(2026, 11, 2, 12, tzinfo=UTC),
        fetch=fetch,
        last_start=lambda _w: datetime(2026, 11, 2, 11, tzinfo=UTC),
        open_card=lambda card: opened.append(card) or 899,
        close_card=lambda task_id, reason: closed.append((task_id, reason)),
        emit=lambda payload: heartbeats.append(dict(payload)),
        workflows=("release-gate.yml", "main-push.yml"),
        liveness=(LivenessStream("nightly", "refresh-test-durations.yml", NIGHTLY_STALE_AFTER),),
    )

    assert asked == ["release-gate.yml", "main-push.yml"]
    assert "stream main-push.yml unreadable" in line
    assert "HTTP 502" in line
    assert heartbeats == [{"fingerprints": 1, "confirmed": 0, "infra": 0, "stale": 1, "unnamed": 0}]
    assert len(load_state(tmp_path / STATE_FILE).entries) == 1


# meiva:note
# The count that watches the watch. An unnameable red opens one card and every repeat of the same
# shape only increments it, so the card count stays quiet while the underlying gap -- a step no
# mapping covers -- keeps producing reds. `unnamed` is the only place that still shows up.
def test_the_round_counts_the_reds_it_could_not_name(tmp_path: Path) -> None:
    heartbeats: list[dict[str, object]] = []
    failure = Failure(
        "Full static checks (structure): unnamed failure (Validate source notes)", FAILURE_UNNAMED
    )
    red = RunFacts(
        "release-gate.yml", "sha1", datetime(2026, 11, 7, 15, 36, tzinfo=UTC), "failure", (failure,)
    )
    second = RunFacts(
        "release-gate.yml", "sha2", datetime(2026, 11, 7, 21, 36, tzinfo=UTC), "failure", (failure,)
    )

    line = watch_once(
        state_path=tmp_path / STATE_FILE,
        now=datetime(2026, 11, 8, 0, tzinfo=UTC),
        fetch=lambda _w: (red, second),
        last_start=lambda _w: datetime(2026, 11, 7, 22, tzinfo=UTC),
        open_card=lambda card: 900,
        close_card=lambda task_id, reason: None,
        emit=lambda payload: heartbeats.append(dict(payload)),
        workflows=("release-gate.yml",),
        liveness=(),
    )

    assert "unnamed=2" in line
    assert heartbeats[0]["unnamed"] == 2
    assert heartbeats[0]["confirmed"] == 1


# meiva:note
# The liveness read is a stream read too: a 502 while asking when a schedule last started must not
# take the round down either.
def test_an_unreadable_liveness_stream_does_not_stop_the_round(tmp_path: Path) -> None:
    heartbeats: list[dict[str, object]] = []

    def last_start(workflow: str) -> datetime | None:
        raise RuntimeError(f"gh api repos/o/r/actions/workflows/{workflow}/runs failed: HTTP 502")

    line = watch_once(
        state_path=tmp_path / STATE_FILE,
        now=datetime(2026, 11, 2, 12, tzinfo=UTC),
        fetch=lambda _w: (),
        last_start=last_start,
        open_card=lambda _c: 1,
        close_card=lambda _t, _r: None,
        emit=lambda payload: heartbeats.append(dict(payload)),
        workflows=(),
        liveness=(LivenessStream("nightly", "refresh-test-durations.yml", NIGHTLY_STALE_AFTER),),
    )

    assert "stream refresh-test-durations.yml unreadable" in line
    assert heartbeats == [{"fingerprints": 0, "confirmed": 0, "infra": 0, "stale": 1, "unnamed": 0}]


# meiva:note
# The slot round end to end: a cron that stopped arriving opens one card, one make-up dispatch
# answers it, and the next scheduled run closes the card. The dispatch is what makes the card
# actionable -- repairing the coverage -- while the card itself waits for the cron to recover.
def _slot_round(
    tmp_path: Path,
    *,
    hours_ago: float | None,
    busy: bool = False,
    uncertified: int | None = 3,
    opened: list[Card],
    closed: list[tuple[int, str]],
    now: datetime | None = None,
) -> str | None:
    clock = now or datetime(2026, 11, 4, 6, tzinfo=UTC)

    def open_card(card: Card) -> int:
        opened.append(card)
        return 700 + len(opened)

    def close_card(task_id: int, reason: str) -> None:
        closed.append((task_id, reason))

    start = None if hours_ago is None else clock - timedelta(hours=hours_ago)
    return slot_round(
        state_path=tmp_path / STATE_FILE,
        now=clock,
        facts=SlotFacts(last_schedule_start=start, busy=busy, uncertified_commits=uncertified),
        open_card=open_card,
        close_card=close_card,
        since=clock - timedelta(hours=13),
    )


def test_a_missing_slot_opens_one_card_across_rounds(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []

    note = _slot_round(tmp_path, hours_ago=13.5, opened=opened, closed=closed)
    _slot_round(
        tmp_path,
        hours_ago=13.5,
        opened=opened,
        closed=closed,
        now=datetime(2026, 11, 4, 6, 30, tzinfo=UTC),
    )

    assert len(opened) == 1, "the second round must not open a second card"
    assert note is not None
    assert load_state(tmp_path / STATE_FILE).slot.task_id == 701


def test_a_scheduled_run_closes_the_slot_card(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []

    _slot_round(tmp_path, hours_ago=13.5, opened=opened, closed=closed)
    _slot_round(tmp_path, hours_ago=0.5, opened=opened, closed=closed)

    assert [task for task, _reason in closed] == [701]
    assert load_state(tmp_path / STATE_FILE).slot.task_id is None


# meiva:note
# A slot the gate would have skipped is not a gap: `release-gate.yml` certifies only when
# `channel/latest` trails `main`, so "nothing uncertified" and "no red" are both silence. The card
# that already exists still closes, because its subject -- unchecked commits -- is gone.
def test_a_slot_with_nothing_uncertified_closes_the_card_without_opening_another(
    tmp_path: Path,
) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []

    _slot_round(tmp_path, hours_ago=13.5, opened=opened, closed=closed)
    note = _slot_round(tmp_path, hours_ago=13.0, uncertified=0, opened=opened, closed=closed)

    assert len(opened) == 1
    assert [task for task, _reason in closed] == [701]
    assert note is not None and "nothing uncertified" in note


def test_a_running_release_gate_opens_no_card(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []

    note = _slot_round(tmp_path, hours_ago=13.5, busy=True, opened=opened, closed=closed)

    assert opened == []
    assert note == "release-gate is running; the slot check waits"


# meiva:note
# The card's heading promises the commits from the last green to the latest red, and the round used
# to be handed the red run's own commit alone under it. A successful full-suite run is what makes
# that base available -- it deletes the entry it resolves, so the sha is kept beside the watermarks
# -- and the range is asked for on the round the red is confirmed.
def test_a_confirmed_red_lists_the_commits_since_the_last_green(tmp_path: Path) -> None:
    opened: list[Card] = []
    asked: list[tuple[str, str]] = []

    def compare(base: str, head: str) -> tuple[str, ...]:
        asked.append((base, head))
        return ("cafe newest", "babe middle", "f00d oldest")

    def round_once(runs: Sequence[RunFacts]) -> str:
        return watch_once(
            state_path=tmp_path / STATE_FILE,
            now=datetime(2026, 11, 2, 12, tzinfo=UTC),
            fetch=lambda _workflow: tuple(runs),
            last_start=lambda _workflow: datetime(2026, 11, 2, 11, tzinfo=UTC),
            open_card=lambda card: (opened.append(card), 899 + len(opened))[1],
            close_card=lambda _task, _reason: None,
            emit=lambda _payload: None,
            workflows=("release-gate.yml",),
            liveness=_LIVENESS,
            compare=compare,
        )

    round_once([_green("release-gate.yml", "a" * 40, datetime(2026, 11, 1, 6, tzinfo=UTC))])
    for hour, sha in ((12, "b" * 40), (18, "c" * 40)):
        round_once([_red("release-gate.yml", sha, datetime(2026, 11, 1, hour, tzinfo=UTC))])

    assert asked == [("a" * 40, "c" * 40)]
    assert len(opened) == 1
    assert "cafe newest" in opened[0].body
    assert f"Last green: `{'a' * 40}`" in opened[0].body


# meiva:note
# A compare is a read and fails like one. The card still has to open -- the red is real whether or
# not its range could be fetched -- so the round falls back to the red run's own line and says in
# its notes that the range was unreadable, rather than filling the heading with what it happens to
# hold and letting a reader believe the list is complete.
def test_an_unreadable_range_still_opens_the_card_and_says_so(tmp_path: Path) -> None:
    opened: list[Card] = []

    def compare(base: str, head: str) -> tuple[str, ...]:
        del base, head
        raise RuntimeError("compare API answered 502")

    def round_once(runs: Sequence[RunFacts]) -> str:
        return watch_once(
            state_path=tmp_path / STATE_FILE,
            now=datetime(2026, 11, 2, 12, tzinfo=UTC),
            fetch=lambda _workflow: tuple(runs),
            last_start=lambda _workflow: datetime(2026, 11, 2, 11, tzinfo=UTC),
            open_card=lambda card: (opened.append(card), 899 + len(opened))[1],
            close_card=lambda _task, _reason: None,
            emit=lambda _payload: None,
            workflows=("release-gate.yml",),
            liveness=_LIVENESS,
            compare=compare,
        )

    round_once([_green("release-gate.yml", "a" * 40, datetime(2026, 11, 1, 6, tzinfo=UTC))])
    line = ""
    for hour, sha in ((12, "b" * 40), (18, "c" * 40)):
        line = round_once([_red("release-gate.yml", sha, datetime(2026, 11, 1, hour, tzinfo=UTC))])

    assert len(opened) == 1
    assert "unreadable: RuntimeError" in line
    assert "c" * 10 in opened[0].body


# meiva:note
# A slot leg that cannot be read is not a quiet slot. Skipping it keeps the round and its heartbeat
# alive, which is right -- but three quiet rounds in a row would otherwise be indistinguishable from
# a gate that keeps its schedule. The count lives in the ledger so the round says so, and the first
# readable round afterwards says how long it was dark.
def test_an_unreadable_slot_leg_reports_itself_instead_of_going_quiet(tmp_path: Path) -> None:
    path = tmp_path / STATE_FILE
    now = datetime(2026, 11, 2, 12, tzinfo=UTC)
    quiet = [
        slot_round(
            state_path=path,
            now=now,
            facts=None,
            open_card=lambda _card: 1,
            close_card=lambda _task, _reason: None,
            unreadable="RuntimeError: 502",
        )
        for _ in range(3)
    ]
    back = slot_round(
        state_path=path,
        now=now,
        facts=SlotFacts(
            last_schedule_start=now - timedelta(hours=1),
            busy=False,
            uncertified_commits=2,
            scheduled_starts=(now - timedelta(hours=1),),
            cadence=RELEASE_GATE_SLOT_CADENCE,
        ),
        open_card=lambda _card: 1,
        close_card=lambda _task, _reason: None,
    )

    assert quiet[:2] == [None, None]
    assert quiet[2] is not None and "3 consecutive rounds" in quiet[2]
    assert back is not None and "readable again after 3 unreadable" in back


# meiva:note
# The ledger that says how long the slot leg has been dark must not cost the watch its memory of
# the last green. The positional rebuild this replaced dropped every map added after it was
# written, so a schedule that collected a range correctly could still find it gone next round.
def test_a_slot_round_keeps_the_memory_of_the_last_green(tmp_path: Path) -> None:
    path = tmp_path / STATE_FILE
    save_state(path, WatchState({}, {}, {}, greens={"release-gate.yml": "a" * 40}))

    slot_round(
        state_path=path,
        now=datetime(2026, 11, 2, 12, tzinfo=UTC),
        facts=None,
        open_card=lambda _card: 1,
        close_card=lambda _task, _reason: None,
        unreadable="RuntimeError: 502",
    )

    assert load_state(path).greens == {"release-gate.yml": "a" * 40}


# meiva:note
# The create key names the occurrence, never the wording. Reworded text under the same occurrence
# has to replay: the round that retries a failed create rebuilds the card from the same facts, and
# a key that followed the text would turn that retry into a second, refused create.
def test_the_create_key_follows_the_occurrence_not_the_wording() -> None:
    card = Card("release-gate.yml/tests/test_a.py::test_x", "title", "body", "2026-11-04T16:00:00")

    reworded = replace(card, title="another title", body="another body")

    assert card_operation_key(reworded) == card_operation_key(card)
    assert card_operation_key(replace(card, episode="2026-11-05T16:00:00")) != card_operation_key(
        card
    )


# meiva:note
# A test id can be longer than the registry's 128-character key limit on its own, so the key hashes
# what it names instead of pasting it: the same card always produces the same key, and no card can
# produce one the registry refuses.
def test_the_create_key_fits_the_registry_limit_for_any_test_id() -> None:
    card = Card("release-gate.yml/" + "tests/test_a.py::test_x" * 20, "title", "body", "episode")

    key = card_operation_key(card)

    assert 1 <= len(key) <= 128
    assert card_operation_key(card) == key
