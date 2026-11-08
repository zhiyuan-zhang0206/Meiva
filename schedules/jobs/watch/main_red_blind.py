# meiva:note
# The effect half of the blindness card: what the watch remembers between rounds so that a run of
# rounds is one window, and what it does when the window opens or ends. The judgement -- when a
# window is worth a card and what the card says -- lives in `schedules/plans/watch/blind_window.py`;
# this file owns the ledger and the two effects, so a test can drive it without a cluster.
#
# The ledger is per stream as well as per window, because the two triggers read different numbers:
# how many streams failed in this round (a round that judged nothing) and how many consecutive
# rounds one stream has failed (a judgement that has been missing while the rest looked healthy).
# `failed_since` is the round each failing stream started, which is what makes the window's start
# honest when streams join at different times, and `backfilled` is the verdict a recovery round
# reaches: were the runs that landed inside the missed interval actually read afterwards.

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime

from base.host.boundary.json_values import is_object_dict
from schedules.plans.watch.blind_window import (
    BLIND_STREAMS,
    BLIND_STREAM_ROUNDS,
    BlindWindow,
    blind_card,
    blind_close_line,
    blind_due,
)
from schedules.plans.watch.main_red import Card, RunFacts

__all__ = [
    "BLIND_STREAMS",
    "BLIND_STREAM_ROUNDS",
    "BlindLedger",
    "advance_blindness",
    "load_blind",
    "save_blind",
    "stream_errors",
]

# meiva:note
# How much of one stream's error the round event carries. The class name and the first line name the
# fault; the rest is a message that can run to a whole response body, and a payload that grows with
# an exception is a payload that eventually fails its own writer.
_MAX_ERROR_CHARS = 160


# meiva:note
# One window of blindness as the watch remembers it between rounds. `since` and `rounds` belong to
# the window as a whole; `streaks` counts consecutive unreadable rounds per stream and is what tells
# a blip from a judgement that has been missing for hours; `streams` keeps the error each stream
# answered with during this window, which is the evidence the card and its closing line carry.
@dataclass(frozen=True)
class BlindLedger:
    since: str | None = None
    task_id: int | None = None
    rounds: int = 0
    streams: Mapping[str, str] = field(default_factory=dict[str, str])
    streaks: Mapping[str, int] = field(default_factory=dict[str, int])
    failed_since: Mapping[str, str] = field(default_factory=dict[str, str])
    backfilled: Mapping[str, bool] = field(default_factory=dict[str, bool])


# meiva:note
# The ledger is a boundary read: the file may predate it, or carry a value a hand edit broke. Each
# map is accepted only in its own shape, so a malformed ledger costs one round's memory of the
# window rather than the whole state file.
def _text_map(raw: object) -> dict[str, str]:
    if not is_object_dict(raw):
        return {}
    return {name: value for name, value in raw.items() if isinstance(value, str)}


def _count_map(raw: object) -> dict[str, int]:
    if not is_object_dict(raw):
        return {}
    return {
        name: value
        for name, value in raw.items()
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
    }


def _flag_map(raw: object) -> dict[str, bool]:
    if not is_object_dict(raw):
        return {}
    return {name: value for name, value in raw.items() if isinstance(value, bool)}


def load_blind(raw: object) -> BlindLedger:
    if not is_object_dict(raw):
        return BlindLedger()
    since = raw.get("since")
    task_id = raw.get("task_id")
    rounds = raw.get("rounds")
    return BlindLedger(
        since=since if isinstance(since, str) else None,
        task_id=task_id if isinstance(task_id, int) and not isinstance(task_id, bool) else None,
        rounds=rounds
        if isinstance(rounds, int) and not isinstance(rounds, bool) and rounds > 0
        else 0,
        streams=_text_map(raw.get("streams")),
        streaks=_count_map(raw.get("streaks")),
        failed_since=_text_map(raw.get("failed_since")),
        backfilled=_flag_map(raw.get("backfilled")),
    )


def save_blind(ledger: BlindLedger) -> dict[str, object]:
    return {
        "since": ledger.since,
        "task_id": ledger.task_id,
        "rounds": ledger.rounds,
        "streams": dict(ledger.streams),
        "streaks": dict(ledger.streaks),
        "failed_since": dict(ledger.failed_since),
        "backfilled": dict(ledger.backfilled),
    }


# meiva:note
# The per-stream reasons the round event carries, bounded twice: one entry per stream (there are
# four) and `_MAX_ERROR_CHARS` of each. This is the field that turns "stale: 4" from a count into a
# diagnosis -- on 2026-11-08 the four failures were one TypeError whose text lived only in a log
# line nobody reads.
def stream_errors(unreadable: Mapping[str, str]) -> list[dict[str, object]]:
    return [
        {"workflow": workflow, "error": error[:_MAX_ERROR_CHARS]}
        for workflow, error in sorted(unreadable.items())
    ]


# meiva:note
# Was the interval a stream missed actually read afterwards? The listing returns the newest runs in
# one page, so the answer is whether the oldest run this recovery round read is at or before the
# round the stream went unreadable. No runs at all means nothing landed in the interval and there is
# nothing to be missing -- the one case where an empty listing is good news rather than an unknown.
def _covered(runs: Sequence[RunFacts], since: datetime) -> bool:
    if not runs:
        return True
    return min(run.created_at for run in runs) <= since


# meiva:note
# One round's worth of bookkeeping and at most two effects: open one card when a window becomes
# worth reporting, close it when the last stream reads again. Returns the ledger to persist and the
# lines this round's log should carry. A round with every stream readable and no window behind it
# returns the ledger untouched, so the common case writes nothing new.
def advance_blindness(
    ledger: BlindLedger,
    *,
    now: datetime,
    workflows: Sequence[str],
    unreadable: Mapping[str, str],
    runs_read: Mapping[str, Sequence[RunFacts]],
    open_card: Callable[[Card], int],
    close_card: Callable[[int, str], None],
    min_streams: int = BLIND_STREAMS,
    stream_rounds: int = BLIND_STREAM_ROUNDS,
) -> tuple[BlindLedger, tuple[str, ...]]:
    streaks = {
        workflow: ledger.streaks.get(workflow, 0) + 1 if workflow in unreadable else 0
        for workflow in workflows
    }
    failed_since = dict(ledger.failed_since)
    backfilled = dict(ledger.backfilled)
    notes: list[str] = []
    for workflow in workflows:
        if workflow in unreadable:
            failed_since.setdefault(workflow, now.isoformat())
            continue
        began = failed_since.pop(workflow, None)
        if began is None:
            continue
        covered = _covered(runs_read.get(workflow, ()), datetime.fromisoformat(began))
        backfilled[workflow] = covered
        notes.append(
            f"stream {workflow} readable again after {ledger.streaks.get(workflow, 0)} round(s): "
            f"the interval it missed was {'covered' if covered else 'NOT covered'}"
        )
    if not unreadable:
        if ledger.task_id is None:
            return BlindLedger(backfilled=backfilled), tuple(notes)
        close_card(
            ledger.task_id,
            blind_close_line(
                datetime.fromisoformat(ledger.since or now.isoformat()),
                now,
                rounds=ledger.rounds,
                streams=sorted(ledger.streams),
                backfilled=backfilled,
            ),
        )
        notes.append(
            f"every stream readable again after {ledger.rounds} round(s): the blindness card closed"
        )
        return BlindLedger(backfilled=backfilled), tuple(notes)
    since = ledger.since or now.isoformat()
    task_id = ledger.task_id
    if task_id is None:
        due = blind_due(
            tuple(unreadable),
            streaks,
            total_streams=len(workflows),
            min_streams=min_streams,
            stream_rounds=stream_rounds,
        )
        if due is not None:
            window = BlindWindow(
                started_at=min(datetime.fromisoformat(failed_since[name]) for name in unreadable),
                streams=tuple(sorted(unreadable.items())),
                rounds=max(streaks[name] for name in unreadable),
                total_streams=len(workflows),
                reason=due,
            )
            task_id = open_card(blind_card(window))
            notes.append(f"blind: {due} -- the watch opened one card for the window")
    return (
        replace(
            ledger,
            since=since,
            task_id=task_id,
            rounds=ledger.rounds + 1 if ledger.since is not None else 1,
            streams={**ledger.streams, **unreadable},
            streaks=streaks,
            failed_since=failed_since,
            backfilled=backfilled,
        ),
        tuple(notes),
    )
