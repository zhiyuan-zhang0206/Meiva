# meiva:note
# The main-red watch's effect shell, driven by a stand-in for `gh` and a recording stand-in for
# the task API: what must hold is that one confirmed red opens exactly one card, a later green run
# closes it, and a round always leaves a heartbeat.
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from base.db import Database
from schedules.jobs.watch.main_red_liveness import liveness_once
from schedules.jobs.watch.main_red_watch import (
    LivenessStream,
    STATE_FILE,
    load_state,
    watch_once,
)
from schedules.plans.watch.main_red import (
    FAILURE_TEST,
    NIGHTLY_STALE_AFTER,
    RELEASE_GATE_STALE_AFTER,
    Card,
    RunFacts,
)

# meiva:note
# The three streams whose silence is reported, as the schedule entry wires them: the helper below
# drives the same table so a test cannot pass against a shape production does not use.
_LIVENESS = (
    LivenessStream("release-gate", "release-gate.yml", RELEASE_GATE_STALE_AFTER),
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
    title: str = "",
) -> RunFacts:
    from schedules.plans.watch.main_red import Failure

    return RunFacts(
        workflow,
        sha,
        when,
        "failure",
        (Failure(test_id, FAILURE_TEST),),
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
        quiet_hours = 12 if workflow == "release-gate.yml" else 2
        return now - timedelta(hours=quiet_hours)

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
    )

    assert opened == []


# meiva:note
# A schedule that never ran is the manager's problem, not this card's: a fresh site must not open a
# liveness card the first time the check runs.
def test_a_watch_with_no_round_on_record_opens_nothing() -> None:
    opened: list[Card] = []

    liveness_once(
        datetime(2026, 11, 3, 10, 35, tzinfo=UTC),
        database=cast("Database", _Database(None)),
        open_card=lambda card: opened.append(card) or 900,
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
        liveness=(LivenessStream("release-gate", "release-gate.yml", RELEASE_GATE_STALE_AFTER),),
    )

    assert asked == ["release-gate.yml", "main-push.yml"]
    assert "stream main-push.yml unreadable" in line
    assert "HTTP 502" in line
    assert heartbeats == [{"fingerprints": 1, "confirmed": 0, "infra": 0, "stale": 1}]
    assert len(load_state(tmp_path / STATE_FILE).entries) == 1


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
    assert heartbeats == [{"fingerprints": 0, "confirmed": 0, "infra": 0, "stale": 1}]
