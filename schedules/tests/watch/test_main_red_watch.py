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
    ATTEMPT_GREEN,
    no_opportunity_notes,
    suspect_rows,
    ATTEMPT_HOLDS,
    ATTEMPT_PENDING,
    AttemptOf,
    LivenessStream,
    already_read,
    job_attempt_verdict,
    listing_attempt,
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
    STATE_CONFIRMED,
    STATE_SUSPECTED,
    Entry,
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
    job: str = "",
) -> RunFacts:
    from schedules.plans.watch.main_red import Failure

    return RunFacts(
        workflow,
        sha,
        when,
        "failure",
        (Failure(test_id, FAILURE_TEST, job=job, covers_full_suite=not partial_job),),
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
    attempt_of: AttemptOf | None = None,
) -> str:
    def fetch(
        _workflow: str, _seen_at: datetime | None, _seen_attempt: int | None
    ) -> tuple[RunFacts, ...]:
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
        attempt_of=attempt_of,
        notify=None,
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
        fetch=lambda _w, _s=None, _a=None: (),
        last_start=last_start,
        open_card=lambda _c: 1,
        close_card=lambda _t, _r: None,
        emit=lambda _p: None,
        workflows=("release-gate.yml",),
        liveness=_LIVENESS,
        notify=None,
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
        fetch=lambda _w, _s=None, _a=None: (),
        open_card=lambda _c: 1,
        close_card=lambda _t, _r: None,
        emit=lambda _p: None,
        last_start=lambda workflow: second - timedelta(hours=30) if workflow == "ci.yml" else None,
        workflows=(),
        liveness=(LivenessStream("nightly-full", "ci.yml", NIGHTLY_STALE_AFTER),),
        notify=None,
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
            fetch=lambda _w, _s=None, _a=None: (),
            last_start=lambda _w: None,
            open_card=lambda _c: 1,
            close_card=lambda _t, _r: None,
            emit=lambda _p: None,
            workflows=(),
            liveness=(LivenessStream("nightly-full", "ci.yml", NIGHTLY_STALE_AFTER),),
            notify=None,
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

    def fetch(
        workflow: str, _seen_at: datetime | None, _seen_attempt: int | None
    ) -> tuple[RunFacts, ...]:
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
        notify=None,
    )

    assert asked == ["release-gate.yml", "main-push.yml"]
    assert "stream main-push.yml unreadable" in line
    assert "HTTP 502" in line
    assert heartbeats[0] | {"suspects": None, "suspects_dropped": None} == {
        "fingerprints": 1,
        "confirmed": 0,
        "infra": 0,
        "stale": 1,
        "unnamed": 0,
        "suspects": None,
        "suspects_dropped": None,
    }
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
        fetch=lambda _w, _s=None, _a=None: (red, second),
        last_start=lambda _w: datetime(2026, 11, 7, 22, tzinfo=UTC),
        open_card=lambda card: 900,
        close_card=lambda task_id, reason: None,
        emit=lambda payload: heartbeats.append(dict(payload)),
        workflows=("release-gate.yml",),
        liveness=(),
        notify=None,
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
        fetch=lambda _w, _s=None, _a=None: (),
        last_start=last_start,
        open_card=lambda _c: 1,
        close_card=lambda _t, _r: None,
        emit=lambda payload: heartbeats.append(dict(payload)),
        workflows=(),
        liveness=(LivenessStream("nightly", "refresh-test-durations.yml", NIGHTLY_STALE_AFTER),),
        notify=None,
    )

    assert "stream refresh-test-durations.yml unreadable" in line
    assert heartbeats[0]["fingerprints"] == 0
    assert heartbeats[0]["stale"] == 1
    assert heartbeats[0]["suspects"] == []


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
            fetch=lambda _workflow, _s=None, _a=None: tuple(runs),
            last_start=lambda _workflow: datetime(2026, 11, 2, 11, tzinfo=UTC),
            open_card=lambda card: (opened.append(card), 899 + len(opened))[1],
            close_card=lambda _task, _reason: None,
            emit=lambda _payload: None,
            workflows=("release-gate.yml",),
            liveness=_LIVENESS,
            compare=compare,
            notify=None,
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
            fetch=lambda _workflow, _s=None, _a=None: tuple(runs),
            last_start=lambda _workflow: datetime(2026, 11, 2, 11, tzinfo=UTC),
            open_card=lambda card: (opened.append(card), 899 + len(opened))[1],
            close_card=lambda _task, _reason: None,
            emit=lambda _payload: None,
            workflows=("release-gate.yml",),
            liveness=_LIVENESS,
            compare=compare,
            notify=None,
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


# meiva:note
# A re-run keeps the run's id and `created_at` and only advances `run_attempt`. The second attempt
# is new evidence — it may repeat the red on the same commit, which is the second sighting that
# confirms a named failure, so the watermark has to carry the attempt number (task #6488). Before
# this, attempt 2 was invisible: the same red counted once, and its recovery could not close a card.
def test_a_second_attempt_of_the_same_run_is_read_once_and_counts(tmp_path: Path) -> None:
    when = datetime(2026, 11, 2, 10, tzinfo=UTC)
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []

    first = replace(_red("release-gate.yml", "a" * 40, when), run_id=701, attempt=1)
    _watch(tmp_path, [first], opened=opened, closed=closed, heartbeats=heartbeats)
    state = load_state(tmp_path / STATE_FILE)
    assert [entry.red_count for entry in state.entries.values()] == [1]
    assert opened == [], "one sighting of a named failure is not yet a card"

    again = replace(first, attempt=2)
    _watch(tmp_path, [again], opened=opened, closed=closed, heartbeats=heartbeats)
    state = load_state(tmp_path / STATE_FILE)
    assert [entry.red_count for entry in state.entries.values()] == [2], "attempt 2 is evidence"
    assert len(opened) == 1, "the second sighting confirms the card"

    _watch(tmp_path, [again], opened=opened, closed=closed, heartbeats=heartbeats)
    state = load_state(tmp_path / STATE_FILE)
    assert [entry.red_count for entry in state.entries.values()] == [2], "a round is idempotent"
    assert len(opened) == 1, "the same attempt never opens a second card"


# meiva:note
# A ledger written before the watermark carried an attempt holds a bare ISO timestamp, and it means
# "that run was read, at whatever attempt it then had". Reading it again would count one red twice
# and could confirm a card from a single event, so the old boundary run stays read (task #6488).
def test_an_old_watermark_does_not_replay_its_boundary_run(tmp_path: Path) -> None:
    when = datetime(2026, 11, 2, 10, tzinfo=UTC)
    path = tmp_path / STATE_FILE
    legacy = _red("release-gate.yml", "b" * 40, when)
    fetch = [legacy]

    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []
    _watch(tmp_path, fetch, opened=opened, closed=closed, heartbeats=heartbeats)

    raw = load_state(path)
    legacy_watermarks = {name: stamp.split("|")[0] for name, stamp in raw.watermarks.items()}
    save_state(path, replace(raw, watermarks=legacy_watermarks))

    _watch(
        tmp_path, [replace(legacy, attempt=2)], opened=opened, closed=closed, heartbeats=heartbeats
    )
    state = load_state(path)
    assert [entry.red_count for entry in state.entries.values()] == [1], (
        "the old run is not replayed"
    )
    assert opened == []


# meiva:note
# The two shards of the case that produced this gate: attempt 1 of `42418111597` had
# `e2e full matrix (1/4)` red and attempt 2 cleared it, while `(3/4)` stayed red. So the question is
# asked of one job in one attempt -- never of the run's own conclusion, which was `failure` in both.
_GATED_JOB = "e2e full matrix (1/4)"
_HOLDS_JOB = "e2e full matrix (3/4)"


def test_only_a_green_attempt_withdraws_a_claim() -> None:
    jobs: list[Mapping[str, object]] = [
        {"name": _GATED_JOB, "conclusion": "success"},
        {"name": _HOLDS_JOB, "conclusion": "failure"},
        {"name": "still running", "conclusion": None},
        {"name": "cancelled", "conclusion": "cancelled"},
    ]

    assert job_attempt_verdict(jobs, _GATED_JOB) == (ATTEMPT_GREEN, "success")
    assert job_attempt_verdict(jobs, _HOLDS_JOB) == (ATTEMPT_HOLDS, "failure")
    assert job_attempt_verdict(jobs, "still running") == (ATTEMPT_PENDING, "no conclusion yet")
    assert job_attempt_verdict(jobs, "cancelled")[0] == ATTEMPT_HOLDS


# meiva:note
# A job the newest attempt does not list is not a job that passed: a re-run can drop a matrix leg,
# and answer `holds` -- "there is a newer attempt" is not "the newer attempt is green".
def test_a_job_missing_from_the_attempt_keeps_the_claim() -> None:
    verdict, seen = job_attempt_verdict(
        [{"name": "another job", "conclusion": "success"}], _HOLDS_JOB
    )

    assert verdict == ATTEMPT_HOLDS
    assert "missing" in seen


def test_the_listing_attempt_defaults_to_the_first() -> None:
    assert listing_attempt({"run_attempt": 2}) == 2
    assert listing_attempt({}) == 1
    assert listing_attempt({"run_attempt": 0}) == 1
    assert listing_attempt({"run_attempt": "3"}) == 1
    assert listing_attempt({"run_attempt": True}) == 1


def _attempt_port(
    jobs: Sequence[Mapping[str, object]], *, attempt: int = 2
) -> tuple[AttemptOf, list[int]]:
    calls: list[int] = []

    def port(run_id: int) -> tuple[int, tuple[Mapping[str, object], ...]]:
        calls.append(run_id)
        return attempt, tuple(jobs)

    return port, calls


# meiva:note
# Two red runs confirm a named failure, and the card is opened for the second one. That run was
# re-run before the round reached it, and the job it named passed in attempt 2 -- so the card must
# not appear, and the entry goes with the claim: a later red of the same case is a new sighting,
# not a second strike earned by a run whose own re-run said it passed.
def test_a_re_run_that_turned_the_job_green_opens_no_card(tmp_path: Path) -> None:
    opened: list[Card] = []
    port, calls = _attempt_port([{"name": _GATED_JOB, "conclusion": "success"}])
    lines = [
        _watch(
            tmp_path,
            [
                replace(
                    _red(
                        "release-gate.yml",
                        "sha1",
                        datetime(2026, 11, 2, 6, tzinfo=UTC),
                        job=_GATED_JOB,
                    ),
                    run_id=701,
                )
            ],
            opened=opened,
            closed=[],
            heartbeats=[],
            attempt_of=port,
        ),
        _watch(
            tmp_path,
            [
                replace(
                    _red(
                        "release-gate.yml",
                        "sha2",
                        datetime(2026, 11, 2, 12, tzinfo=UTC),
                        job=_GATED_JOB,
                    ),
                    run_id=702,
                )
            ],
            opened=opened,
            closed=[],
            heartbeats=[],
            attempt_of=port,
        ),
    ]

    assert opened == [], "the newest attempt of its own run cleared the red"
    assert calls == [702], "only the run that would have opened the card is asked about"
    assert "attempt 2 green" in lines[-1]
    assert load_state(tmp_path / STATE_FILE).entries == {}, "the claim is withdrawn with the entry"


# meiva:note
# The reverse control: the same two rounds with the job still red in attempt 2 must open the card,
# or the previous test would pass for a gate that never fires.
def test_a_re_run_that_is_still_red_opens_the_card(tmp_path: Path) -> None:
    opened: list[Card] = []
    port, _calls = _attempt_port([{"name": _GATED_JOB, "conclusion": "failure"}])
    for sha, hour, run_id in (("sha1", 6, 701), ("sha2", 12, 702)):
        line = _watch(
            tmp_path,
            [
                replace(
                    _red(
                        "release-gate.yml",
                        sha,
                        datetime(2026, 11, 2, hour, tzinfo=UTC),
                        job=_GATED_JOB,
                    ),
                    run_id=run_id,
                )
            ],
            opened=opened,
            closed=[],
            heartbeats=[],
            attempt_of=port,
        )

    assert len(opened) == 1
    assert "attempt 2 failure" in line


# meiva:note
# An attempt that has not concluded settles nothing: it must not open a card (the re-run may still
# clear it) and must not withdraw the claim (nobody has read a green). The entry stays confirmed and
# the next round asks again.
def test_an_unfinished_attempt_waits_instead_of_deciding(tmp_path: Path) -> None:
    opened: list[Card] = []
    port, _calls = _attempt_port([{"name": _GATED_JOB, "conclusion": None}])
    for sha, hour, run_id in (("sha1", 6, 701), ("sha2", 12, 702)):
        line = _watch(
            tmp_path,
            [
                replace(
                    _red(
                        "release-gate.yml",
                        sha,
                        datetime(2026, 11, 2, hour, tzinfo=UTC),
                        job=_GATED_JOB,
                    ),
                    run_id=run_id,
                )
            ],
            opened=opened,
            closed=[],
            heartbeats=[],
            attempt_of=port,
        )

    assert opened == []
    assert "card waits" in line
    entries = load_state(tmp_path / STATE_FILE).entries
    (entry,) = entries.values()
    assert entry.state == STATE_CONFIRMED
    assert entry.task_id is None


# meiva:note
# A read that failed must not answer "safe": the gate suppresses a card only on a green someone
# actually read, so an unreadable attempt opens the card and says so in the round line.
def test_an_unreadable_attempt_still_opens_the_card(tmp_path: Path) -> None:
    opened: list[Card] = []

    def port(_run_id: int) -> tuple[int, tuple[Mapping[str, object], ...]]:
        raise RuntimeError("gh api failed")

    for sha, hour, run_id in (("sha1", 6, 701), ("sha2", 12, 702)):
        line = _watch(
            tmp_path,
            [
                replace(
                    _red(
                        "release-gate.yml",
                        sha,
                        datetime(2026, 11, 2, hour, tzinfo=UTC),
                        job=_GATED_JOB,
                    ),
                    run_id=run_id,
                )
            ],
            opened=opened,
            closed=[],
            heartbeats=[],
            attempt_of=port,
        )

    assert len(opened) == 1
    assert "unreadable" in line


# meiva:note
# An entry written before `Entry.job` existed keeps no job, and the gate stays out of its way: the
# red it stands for was decided without the attempt evidence, and nothing here may re-decide it by
# guessing which job it belonged to.
def test_an_entry_without_a_job_skips_the_gate(tmp_path: Path) -> None:
    opened: list[Card] = []
    port, calls = _attempt_port([{"name": _GATED_JOB, "conclusion": "success"}])
    for sha, hour, run_id in (("sha1", 6, 701), ("sha2", 12, 702)):
        _watch(
            tmp_path,
            [
                replace(
                    _red("release-gate.yml", sha, datetime(2026, 11, 2, hour, tzinfo=UTC)),
                    run_id=run_id,
                )
            ],
            opened=opened,
            closed=[],
            heartbeats=[],
            attempt_of=port,
        )

    assert len(opened) == 1
    assert calls == [], "no job on the entry means no question to ask"


def test_the_state_file_carries_the_job_across_rounds(tmp_path: Path) -> None:
    path = tmp_path / STATE_FILE
    _watch(
        path.parent,
        [
            replace(
                _red(
                    "release-gate.yml", "sha1", datetime(2026, 11, 2, 6, tzinfo=UTC), job=_GATED_JOB
                ),
                run_id=701,
            )
        ],
        opened=[],
        closed=[],
        heartbeats=[],
    )

    (entry,) = load_state(path).entries.values()
    assert entry.job == _GATED_JOB


# meiva:note
# The other half of the attempt gate: a card opened before its run was re-run had no exit a re-run
# could reach -- `apply_run` retires a card on a later green run, and the run that would have to be
# green is the one already read (task #6488). The port below answers per run, so the same two rounds
# can open a card and then watch attempt 2 of that same run turn it green.
class _Attempts:
    def __init__(self, verdict: str, seen: str, attempt: int = 2) -> None:
        self.verdict = verdict
        self.seen = seen
        self.attempt = attempt
        self.calls: list[int] = []

    def __call__(self, run_id: int) -> tuple[int, tuple[Mapping[str, object], ...]]:
        self.calls.append(run_id)
        return self.attempt, ({"name": _GATED_JOB, "conclusion": self.seen},)


def _two_rounds(
    tmp_path: Path, port: AttemptOf, *, opened: list[Card]
) -> tuple[list[tuple[int, str]], list[str]]:
    closed: list[tuple[int, str]] = []
    lines: list[str] = []
    for sha, hour, run_id in (("sha1", 6, 701), ("sha2", 12, 702)):
        lines.append(
            _watch(
                tmp_path,
                [
                    replace(
                        _red(
                            "release-gate.yml",
                            sha,
                            datetime(2026, 11, 2, hour, tzinfo=UTC),
                            job=_GATED_JOB,
                        ),
                        run_id=run_id,
                    )
                ],
                opened=opened,
                closed=closed,
                heartbeats=[],
                attempt_of=port,
            )
        )
    return closed, lines


def test_a_card_closes_when_its_own_run_is_re_run_green(tmp_path: Path) -> None:
    opened: list[Card] = []
    port = _Attempts("holds", "failure")
    closed, _lines = _two_rounds(tmp_path, port, opened=opened)
    assert len(opened) == 1, "the first two reds confirm the card"
    assert closed == []

    port.verdict, port.seen = "green", "success"
    port.calls.clear()
    line = _watch(
        tmp_path,
        [],
        opened=opened,
        closed=closed,
        heartbeats=[],
        attempt_of=port,
    )

    assert closed == [(900, "e2e full matrix (1/4) passed in attempt 2 of run 702")]
    assert "closed card 900" in line
    assert port.calls == [702], "the card is asked about the run that last reported its red"
    assert load_state(tmp_path / STATE_FILE).entries == {}, "the entry goes with the card"


# meiva:note
# The reverse control: the same third round with the job still red in attempt 2 must leave the card
# open, or the previous test would pass for a re-check that closes on any verdict.
def test_a_card_stays_open_while_the_attempt_is_still_red(tmp_path: Path) -> None:
    opened: list[Card] = []
    port = _Attempts("holds", "failure")
    _two_rounds(tmp_path, port, opened=opened)
    closed: list[tuple[int, str]] = []

    _watch(tmp_path, [], opened=opened, closed=closed, heartbeats=[], attempt_of=port)

    assert closed == []
    assert len(load_state(tmp_path / STATE_FILE).entries) == 1


def test_an_unreadable_attempt_leaves_the_card_open(tmp_path: Path) -> None:
    opened: list[Card] = []
    port = _Attempts("holds", "failure")
    _two_rounds(tmp_path, port, opened=opened)
    closed: list[tuple[int, str]] = []

    def broken(_run_id: int) -> tuple[int, tuple[Mapping[str, object], ...]]:
        raise RuntimeError("gh api failed")

    line = _watch(tmp_path, [], opened=opened, closed=closed, heartbeats=[], attempt_of=broken)

    assert closed == []
    assert "unreadable" in line


# meiva:note
# A card on an entry from a ledger that predates `Entry.job`/`run_id` names no run, and nothing may
# invent one: an older run's green would retire a card the newer red owns.
def test_a_card_without_a_run_id_is_not_asked_about(tmp_path: Path) -> None:
    opened: list[Card] = []
    path = tmp_path / STATE_FILE
    _watch(
        path.parent,
        [
            _red("release-gate.yml", "sha1", datetime(2026, 11, 2, 6, tzinfo=UTC)),
            _red("release-gate.yml", "sha2", datetime(2026, 11, 2, 12, tzinfo=UTC)),
        ],
        opened=opened,
        closed=[],
        heartbeats=[],
    )
    assert len(opened) == 1, "the card is open, and its entry names no job or run"

    port = _Attempts("green", "success")
    closed: list[tuple[int, str]] = []
    _watch(tmp_path, [], opened=opened, closed=closed, heartbeats=[], attempt_of=port)

    assert closed == []
    assert port.calls == [], "no run id means no run to ask about"


# meiva:note
# The boundary is one judgement used twice: the round applies it to a run it has read, and the fetch
# applies it to a listing row before spending a jobs read and a log download on it (task #6509). This
# table is the whole rule -- older is read, newer is not, and at the same instant the attempt decides
# while an unknown attempt means the row counts as read.
def test_the_watermark_boundary_is_the_same_for_a_run_and_a_row() -> None:
    seen = datetime(2026, 11, 2, 12, tzinfo=UTC)
    older = datetime(2026, 11, 2, 11, tzinfo=UTC)

    assert already_read(older, 9, seen, 1) is True
    assert already_read(seen, 1, seen, 1) is True
    assert already_read(seen, 2, seen, 1) is False, "a newer attempt of the same run is unread"
    assert already_read(seen, 1, seen, None) is True, "an unknown attempt means already read"
    assert already_read(datetime(2026, 11, 2, 13, tzinfo=UTC), 1, seen, 9) is False
    assert already_read(older, 1, None, None) is False, "no watermark yet"


# meiva:note
# The port carries the watermark, or the fetch cannot skip anything: this asserts what the round
# hands over, and that the first round hands over nothing at all.
def test_the_round_tells_the_fetch_where_its_watermark_is(tmp_path: Path) -> None:
    seen: list[tuple[datetime | None, int | None]] = []
    when = datetime(2026, 11, 2, 10, tzinfo=UTC)

    def fetch(
        _workflow: str, seen_at: datetime | None, seen_attempt: int | None
    ) -> tuple[RunFacts, ...]:
        seen.append((seen_at, seen_attempt))
        return (replace(_red("release-gate.yml", "a" * 40, when), run_id=701, attempt=2),)

    for _round in range(2):
        watch_once(
            state_path=tmp_path / STATE_FILE,
            now=datetime(2026, 11, 2, 12, tzinfo=UTC),
            fetch=fetch,
            last_start=lambda _workflow: datetime(2026, 11, 2, 11, tzinfo=UTC),
            open_card=lambda _card: 900,
            close_card=lambda _task, _reason: None,
            emit=lambda _payload: None,
            workflows=("release-gate.yml",),
            liveness=_LIVENESS,
            notify=None,
        )

    assert seen[0] == (None, None), "the first round has no watermark to offer"
    assert seen[1] == (when, 2), "the second names the newest (created_at, attempt) it read"


# meiva:note
# The case the task is named for: a red seen once, with no completed run of its workflow after it.
# Nothing will ever confirm it (a second red is what confirms) and nothing will retire it (a green
# run is what retires), so it waits forever unless the round says so (task #6488).
def test_a_suspect_without_a_later_run_reports_no_recheck_opportunity(tmp_path: Path) -> None:
    when = datetime(2026, 11, 2, 6, tzinfo=UTC)
    heartbeats: list[dict[str, object]] = []

    line = _watch(
        tmp_path,
        [_red("release-gate.yml", "sha1", when)],
        opened=[],
        closed=[],
        heartbeats=heartbeats,
    )

    assert "no re-check opportunity" in line
    assert "sha1" not in line, "the line names the fingerprint and the moment, not the commit"
    (entry,) = load_state(tmp_path / STATE_FILE).entries.values()
    assert entry.opportunities == 0
    assert heartbeats[0]["suspects"] == [
        {
            "fingerprint": entry.fingerprint,
            "first_seen_at": when.isoformat(),
            "red_count": 1,
            "opportunities": 0,
        }
    ]


# meiva:note
# A later run that reports some other red is a chance to re-check this one -- the watch read a
# completed run of the same workflow and this case did not appear in it. Counting it is what makes
# the number mean "chances", not "time": the line stays quiet, and the entry keeps its age.
def test_a_later_run_that_does_not_report_the_case_counts_as_a_chance(tmp_path: Path) -> None:
    first = datetime(2026, 11, 2, 6, tzinfo=UTC)
    later = datetime(2026, 11, 2, 12, tzinfo=UTC)
    other = "pkg/tests/test_b.py::test_other"

    _watch(tmp_path, [_red("release-gate.yml", "sha1", first)], opened=[], closed=[], heartbeats=[])
    line = _watch(
        tmp_path,
        [_red("release-gate.yml", "sha2", later, test_id=other)],
        opened=[],
        closed=[],
        heartbeats=[],
    )

    entries = load_state(tmp_path / STATE_FILE).entries
    mine = next(entry for entry in entries.values() if "test_a" in entry.fingerprint)
    assert mine.opportunities == 1
    assert mine.fingerprint not in line, "this one had a chance, so it is not the line's subject"
    assert "test_b.py::test_other" in line, "the other red is the one with no chance yet"


def test_suspect_rows_are_capped_and_say_how_many_were_dropped() -> None:
    when = datetime(2026, 11, 2, 6, tzinfo=UTC)
    entries = {
        f"release-gate.yml::test_{index:02d}": Entry(
            fingerprint=f"release-gate.yml::test_{index:02d}",
            state=STATE_SUSPECTED,
            first_seen_sha="sha1",
            first_seen_at=when + timedelta(minutes=index),
            last_red_sha="sha1",
            last_red_at=when + timedelta(minutes=index),
            red_count=1,
            opportunities=index,
        )
        for index in range(12)
    }

    rows, dropped = suspect_rows(entries, limit=10)

    assert len(rows) == 10
    assert dropped == 2
    assert rows[0]["fingerprint"] == "release-gate.yml::test_00", "oldest first"
    assert rows[0]["opportunities"] == 0


def test_only_suspects_without_a_chance_are_named_and_the_rest_are_counted() -> None:
    rows = [
        {
            "fingerprint": f"w::t{index}",
            "first_seen_at": "2026-11-02T06:00:00+00:00",
            "red_count": 1,
            "opportunities": 0,
        }
        for index in range(5)
    ] + [
        {
            "fingerprint": "w::seen",
            "first_seen_at": "2026-11-02T06:00:00+00:00",
            "red_count": 1,
            "opportunities": 3,
        }
    ]

    lines = no_opportunity_notes(rows, limit=3)

    assert len(lines) == 4, "three named, one line for the remainder"
    assert "2 more suspects" in lines[-1]
    assert not any("w::seen" in line for line in lines)


# meiva:note
# The transition, not the state: the round that gives the watch its first suspect says so once, and
# every round after it is quiet because the suspect is already there. This is the mechanism 405
# asked for instead of a promise to remember (2026-11-08).
def test_the_first_suspect_is_announced_once(tmp_path: Path) -> None:
    when = datetime(2026, 11, 2, 6, tzinfo=UTC)
    said: list[str] = []

    def notify(text: str) -> None:
        said.append(text)

    def round_(runs: list[RunFacts], *, now: datetime) -> None:
        watch_once(
            state_path=tmp_path / STATE_FILE,
            now=now,
            fetch=lambda _workflow, _seen_at, _seen_attempt: tuple(runs),
            last_start=lambda _workflow: now - timedelta(hours=1),
            open_card=lambda _card: 900,
            close_card=lambda _task, _reason: None,
            emit=lambda _payload: None,
            workflows=("release-gate.yml",),
            liveness=_LIVENESS,
            notify=notify,
        )

    round_([], now=when)
    assert said == [], "no entries, nothing to announce"

    round_([_red("release-gate.yml", "sha1", when)], now=when + timedelta(minutes=30))
    assert len(said) == 1
    assert "first suspected red" in said[0]
    assert "test_a" in said[0]

    round_(
        [_red("release-gate.yml", "sha2", when + timedelta(hours=1), test_id="pkg/t/o.py::t_o")],
        now=when + timedelta(hours=1),
    )
    assert len(said) == 1, "a suspect already present announces nothing"
