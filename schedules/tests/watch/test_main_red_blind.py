# meiva:note
# The blindness card: what the watch says when it stops being able to read its own streams. Two
# triggers -- two or more streams unreadable in one round, or one stream unreadable for six
# consecutive rounds -- one card per window, and a closing line that says whether the interval the
# window covered was ever read afterwards. The round line and the round event are asserted here
# too, because a card is only half the fix: the other half is that `stale: 4` stops being the only
# account of what happened.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from schedules.jobs.watch.main_red_watch import STATE_FILE, load_state, save_state, watch_once
from schedules.plans.watch.main_red import Card, RunFacts, STATE_CONFIRMED, Entry
from schedules.jobs.watch.main_red_watch import SlotLedger, WatchState

_STREAMS = ("release-gate.yml", "main-push.yml", "ci.yml", "refresh-test-durations.yml")
_START = datetime(2026, 11, 8, 5, tzinfo=UTC)


def _run(workflow: str, when: datetime, sha: str = "sha1") -> RunFacts:
    return RunFacts(workflow, sha, when, "success", (), covers_full_suite=True)


def _round(
    tmp_path: Path,
    now: datetime,
    *,
    failing: Mapping[str, str],
    opened: list[Card],
    closed: list[tuple[int, str]],
    heartbeats: list[dict[str, object]],
    runs: Mapping[str, Sequence[RunFacts]] | None = None,
    workflows: tuple[str, ...] = _STREAMS,
    blind_min_streams: int = 2,
    blind_stream_rounds: int = 6,
) -> str:
    def fetch(
        workflow: str, _seen_at: datetime | None, _seen_attempt: int | None
    ) -> tuple[RunFacts, ...]:
        if workflow in failing:
            raise RuntimeError(failing[workflow])
        return tuple((runs or {}).get(workflow, ()))

    return watch_once(
        state_path=tmp_path / STATE_FILE,
        now=now,
        fetch=fetch,
        last_start=lambda _workflow: now - timedelta(hours=1),
        open_card=lambda card: opened.append(card) or (900 + len(opened)),
        close_card=lambda task_id, reason: closed.append((task_id, reason)),
        emit=lambda payload: heartbeats.append(dict(payload)),
        workflows=workflows,
        liveness=(),
        notify=None,
        blind_min_streams=blind_min_streams,
        blind_stream_rounds=blind_stream_rounds,
    )


# meiva:note
# The round that could read nothing opens exactly one card, and the next round that reads everything
# closes it with the interval and the backfill verdict. Both halves matter: a card that never closes
# becomes furniture, and a close line that only says "fine again" loses the one fact a later reader
# needs -- whether anything that landed inside the window was ever judged.
def test_two_unreadable_streams_open_one_card_and_the_next_readable_round_closes_it(
    tmp_path: Path,
) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []

    _round(
        tmp_path,
        _START,
        failing={"ci.yml": "boom", "main-push.yml": "HTTP 502"},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )

    assert len(opened) == 1
    card = opened[0]
    assert card.fingerprint == "main-red-watch/blindness"
    assert card.episode == _START.isoformat()
    assert "2 of 4 streams" in card.title
    assert "`ci.yml` — `RuntimeError: boom`" in card.body
    assert "`main-push.yml` — `RuntimeError: HTTP 502`" in card.body
    assert "unjudged, not green" in card.body
    assert heartbeats[0]["unreadable"] == 2
    assert heartbeats[0]["stream_errors"] == [
        {"workflow": "ci.yml", "error": "RuntimeError: boom"},
        {"workflow": "main-push.yml", "error": "RuntimeError: HTTP 502"},
    ]

    _round(
        tmp_path,
        _START + timedelta(minutes=30),
        failing={},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )

    assert len(opened) == 1
    assert [task_id for task_id, _reason in closed] == [901]
    reason = closed[0][1]
    assert reason.startswith("blind window 2026-11-08T05:00:00+00:00 -> 2026-11-08T05:30:00+00:00")
    assert "1 round(s)" in reason
    assert "ci.yml covered" in reason and "main-push.yml covered" in reason


# meiva:note
# One stream failing once is the shape the round was built for: a note, a count, and the other three
# streams still judged. Upgrading that to a card would spend the lead's attention on every busy API,
# which is how a card list stops being read.
def test_one_unreadable_stream_opens_no_card(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []

    line = _round(
        tmp_path,
        _START,
        failing={"main-push.yml": "HTTP 502"},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )

    assert opened == []
    assert "stream main-push.yml unreadable" in line
    assert heartbeats[0]["unreadable"] == 1
    assert load_state(tmp_path / STATE_FILE).blind.task_id is None


# meiva:note
# The second trigger, and the one no round can see: the watch looks healthy -- three streams report,
# the heartbeat lands -- while one judgement has been missing for three hours. The card has to name
# the stream, because "something is blind" is not actionable and "`ci.yml` has not been read since
# 02:30" is.
def test_one_stream_unreadable_for_six_rounds_opens_a_card_that_names_it(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []

    for round_number in range(5):
        _round(
            tmp_path,
            _START + timedelta(minutes=30 * round_number),
            failing={"ci.yml": "boom"},
            opened=opened,
            closed=closed,
            heartbeats=heartbeats,
        )

    assert opened == []

    _round(
        tmp_path,
        _START + timedelta(minutes=30 * 5),
        failing={"ci.yml": "boom"},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )

    assert len(opened) == 1
    assert "`ci.yml` unreadable for 6 rounds" in opened[0].title
    assert "1 of 4 streams" not in opened[0].title


# meiva:note
# A window is one card however it grows: a stream that joins the failure later is the same window,
# and a second card would make the reader reconcile two intervals that overlap.
def test_a_third_stream_joining_the_window_opens_no_second_card(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []

    _round(
        tmp_path,
        _START,
        failing={"ci.yml": "boom", "main-push.yml": "HTTP 502"},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )
    _round(
        tmp_path,
        _START + timedelta(minutes=30),
        failing={"ci.yml": "boom", "main-push.yml": "HTTP 502", "release-gate.yml": "404"},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )

    assert len(opened) == 1
    assert heartbeats[1]["unreadable"] == 3


# meiva:note
# The closing line has to be able to say "not covered". The listing returns the newest runs in one
# page, so a recovery round whose oldest run is newer than the round the stream went unreadable did
# not reach back over the interval -- and calling that covered would be the same mistake as calling
# a blind watch a clean `main`, one layer further in.
def test_a_window_the_listing_could_not_reach_back_over_is_not_called_covered(
    tmp_path: Path,
) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []

    _round(
        tmp_path,
        _START,
        failing={"ci.yml": "boom", "main-push.yml": "HTTP 502"},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )
    _round(
        tmp_path,
        _START + timedelta(minutes=30),
        failing={"ci.yml": "boom"},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
        runs={"main-push.yml": (_run("main-push.yml", _START + timedelta(minutes=20)),)},
    )
    _round(
        tmp_path,
        _START + timedelta(minutes=60),
        failing={},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )

    assert len(closed) == 1
    reason = closed[0][1]
    assert "main-push.yml NOT covered" in reason
    assert "ci.yml covered" in reason


# meiva:note
# The negative control the card's own acceptance needs: a round in which every stream throws. If the
# fixture cannot produce four failures, the trigger is never actually exercised -- a test that asks
# for two and gets one passes for the wrong reason.
def test_a_round_that_could_read_nothing_names_all_four_streams(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []

    _round(
        tmp_path,
        _START,
        failing={workflow: f"{workflow} down" for workflow in _STREAMS},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )

    assert len(opened) == 1
    assert "4 of 4 streams" in opened[0].title
    assert heartbeats[0]["unreadable"] == 4
    assert heartbeats[0]["stream_errors"] == [
        {"workflow": workflow, "error": f"RuntimeError: {workflow} down"}
        for workflow in sorted(_STREAMS)
    ]
    stored = load_state(tmp_path / STATE_FILE)
    assert stored.blind.since == _START.isoformat()
    assert stored.blind.task_id == 901


# meiva:note
# The bug the split exposed: `watch_once` rebuilt the state from the maps it happened to name, so
# every watch round reset the slot ledger, and the slot card met a `task_id` of None on the round
# that should have closed it. A watch round must leave a ledger it does not own untouched.
def test_a_watch_round_keeps_the_slot_ledger(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []
    entry = Entry(
        fingerprint="release-gate.yml/tests/test_a.py::test_x",
        state=STATE_CONFIRMED,
        first_seen_sha="sha1",
        first_seen_at=_START,
        last_red_sha="sha1",
        last_red_at=_START,
        red_count=2,
        task_id=899,
        kind="fail",
        job="job",
        run_id=702,
        opportunities=1,
        covers_full_suite=True,
    )
    save_state(
        tmp_path / STATE_FILE,
        WatchState(
            entries={entry.fingerprint: entry},
            watermarks={},
            first_seen={},
            slot=SlotLedger(gap_id="gap-1", task_id=799, unreadable_rounds=2),
        ),
    )

    _round(
        tmp_path,
        _START,
        failing={},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
    )

    assert load_state(tmp_path / STATE_FILE).slot.task_id == 799


# meiva:note
# The thresholds are configuration, not code: a deployment that wants a round of three failing
# streams to be a card, or a stream to be reported after three rounds instead of six, changes a
# value rather than the judgement. This pins that the caller's numbers are the ones that decide --
# a default that ignores the parameter would pass every test that uses the default.
def test_the_thresholds_are_the_callers(tmp_path: Path) -> None:
    opened: list[Card] = []
    closed: list[tuple[int, str]] = []
    heartbeats: list[dict[str, object]] = []

    _round(
        tmp_path,
        _START,
        failing={"ci.yml": "boom", "main-push.yml": "HTTP 502"},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
        blind_min_streams=4,
    )

    assert opened == []

    _round(
        tmp_path,
        _START + timedelta(minutes=30),
        failing={"ci.yml": "boom"},
        opened=opened,
        closed=closed,
        heartbeats=heartbeats,
        blind_stream_rounds=2,
    )

    assert len(opened) == 1
    assert "`ci.yml` unreadable for 2 rounds" in opened[0].title
