# meiva:note
# The release-gate slot card watches scheduled arrivals, not code health or
# release publication. A manual/PR run or a caught-up channel can suppress a
# new complaint but cannot prove that an existing cron gap recovered. Only a
# newer verified scheduled start ends that occurrence (task #6822).
# The check never dispatches or publishes; its arrival threshold remains nine
# hours, independently of the workflow's six-hour cadence.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from schedules.plans.watch.main_red import Card

__all__ = [
    "RELEASE_GATE_SLOT_CADENCE",
    "RELEASE_GATE_SLOT_LOOKBACK",
    "RELEASE_GATE_SLOT_STALE_AFTER",
    "SlotAction",
    "SlotDecision",
    "SlotFacts",
    "decide_slot",
]

# meiva:note
# How still the gate has to be before a round calls a slot missing.
#
# Two quantities are in play and they must not be confused. The first is lateness: how long after
# the slot it served a run actually started. Measured on 2026-11-04 across every scheduled workflow
# in this repository, 164 scheduled runs arrived 45m to 7h50m after their slot (median 4h34m), so a
# threshold inside that band reports lateness and calls a healthy pair of runs a missing slot. The
# second is the gap between two starts of this workflow, which is what the round can actually see;
# one slot plus that arrival band is an ordinary late pair.
#
# The five most recent release-gate gaps replay as 4h09m, 8h09m, 9h59m, 8h49m and 13h21m, and the
# 9h59m and 13h21m windows are the two that really missed a slot. Seven hours opens four of the
# five -- the 8h09m pair had no missing slot at all, one run simply served its slot 5h57m late.
# Twelve hours opens one and leaves the 9h59m gap unreported. Nine hours opens exactly the two that
# missed.
#
# So it is not a whole number of slots on purpose: the slot length belongs to the workflow's
# schedule, and this constant is a judgement about arrivals. Nine hours sits between the slowest gap
# that was merely late (8h49m) and the fastest gap that really missed a slot (9h59m).
RELEASE_GATE_SLOT_STALE_AFTER = timedelta(hours=9)

# meiva:note
# The workflow's own cadence and how far back the card counts arrivals. These are not the threshold:
# the cadence says how many runs the silence should have contained, which is what lets a reader
# judge one card on its own instead of waiting for an interval that happens to look suspicious.
RELEASE_GATE_SLOT_CADENCE = timedelta(hours=6)
RELEASE_GATE_SLOT_LOOKBACK = timedelta(hours=24)

FINGERPRINT = "main-red-watch/release-gate-slot"
_NO_GAP = ""
# meiva:note
# A gap with no scheduled run behind it still needs a name of its own: an empty id means "no gap",
# and reusing it here would read as "already dispatched" on the first round that noticed.
_NO_RUN = "no-run-on-record"


# meiva:note
# What one round reads about the workflow: the newest run the cron started, and whether anything of
# that workflow is still running. Busy activity suppresses a new complaint;
# it cannot establish recovery of an already recorded scheduled-arrival gap.
@dataclass(frozen=True)
class SlotFacts:
    last_schedule_start: datetime | None
    busy: bool
    uncertified_commits: int | None = None
    channel_latest: str | None = None
    # meiva:note
    # Every scheduled start the round could read, newest first, and the cadence they were due on.
    # Together they let the card state how many arrivals the silence should have contained, which is
    # a fact a reader can check against GitHub instead of having to trust the threshold.
    scheduled_starts: tuple[datetime, ...] = ()
    cadence: timedelta = RELEASE_GATE_SLOT_CADENCE


class SlotAction(StrEnum):
    WAIT = "wait"
    OPEN = "open"
    RECOVERED = "scheduled-recovered"


# meiva:note
# No card is an instruction to wait unless the decision explicitly proves
# recovery. Keeping the distinction prevents busy/uncertified suppression from
# resolving a previously opened scheduled-arrival gap.
@dataclass(frozen=True)
class SlotDecision:
    action: SlotAction
    card: Card | None
    gap_id: str
    note: str | None


def _gap_id(last_start: datetime | None) -> str:
    return "" if last_start is None else last_start.isoformat(timespec="seconds")


def _behind(facts: SlotFacts) -> str:
    if facts.uncertified_commits is None:
        return "The uncertified commit count could not be read this round."
    if facts.channel_latest:
        return (
            f"`channel/latest` is `{facts.channel_latest}`, {facts.uncertified_commits} commit(s) "
            "behind `main`."
        )
    return f"`main` is {facts.uncertified_commits} commit(s) past the last certification."


# meiva:note
# How many arrivals the lookback should have contained, and how many it did. Stated on the card
# because it is the one number a reader can check against the workflow's own run list: a card whose
# count matches what GitHub shows is about a cron that stopped, and one that does not is about a
# reading that failed.
# meiva:note
# How many scheduled starts the lookback should have held and did, counted back from the newest
# start the round read rather than from `now`. Anchoring the window to the observation keeps the
# number a function of what the card is about: counted from `now`, the same missing slot states a
# different count every round, and a card whose text moves under one key cannot be created twice.
def _arrivals(facts: SlotFacts) -> tuple[int, int]:
    expected = int(RELEASE_GATE_SLOT_LOOKBACK / facts.cadence)
    anchor = facts.last_schedule_start
    if anchor is None:
        return 0, expected
    window_start = anchor - RELEASE_GATE_SLOT_LOOKBACK
    observed = sum(1 for started in facts.scheduled_starts if window_start <= started <= anchor)
    return observed, expected


# meiva:note
# The card one missing slot opens. `now` decided that the slot is missing; it is not an input here,
# because everything the card says has to hold for as long as the gap lasts: the newest scheduled
# start and the arrivals before it do, and "9.5h ago" does not.
def _card(facts: SlotFacts, *, threshold: timedelta, gap_id: str) -> Card:
    newest = (
        facts.last_schedule_start.isoformat()
        if facts.last_schedule_start is not None
        else "no scheduled run on record"
    )
    since = (
        facts.last_schedule_start.isoformat()
        if facts.last_schedule_start is not None
        else "this watch's first round"
    )
    observed, expected = _arrivals(facts)
    return Card(
        fingerprint=FINGERPRINT,
        title=f"release-gate has not run on its schedule since {since}",
        body=(
            "**This card publishes nothing.** No tag is minted and `channel/latest` does not "
            "move until a human runs the gate.\n\n"
            f"{_behind(facts)}\n\n"
            f"The newest scheduled `release-gate` run started at {newest}; the threshold is "
            f"{threshold.total_seconds() / 3600:.0f}h.\n\n"
            "This reports the schedule, not the code: `main` has had no full-population check in "
            "that window, so a red merged in it is invisible rather than absent.\n\n"
            f"In the {RELEASE_GATE_SLOT_LOOKBACK.total_seconds() / 3600:.0f}h ending at that run, "
            f"this {facts.cadence.total_seconds() / 3600:.0f}-hourly cron should have started "
            f"{expected} run(s) and started {observed}. Confirm that count against the "
            "workflow's own run list before acting on this card: it is the difference between a "
            "cron that stopped and a round that could not read.\n\n"
            "**What to run instead.** `gh workflow run main-typecheck.yml --ref main` takes about "
            "two and a half minutes and restores the type surface only: the full tree stays "
            "unchecked, and nothing is certified. The full run is "
            "`gh workflow run release-gate.yml --ref main`; it certifies on success, so it releases "
            "-- that is the user's call, not the watch's.\n\n"
            "Why nine hours: across every scheduled workflow in this repository, 164 scheduled runs "
            "arrived 45m to 7h50m after the slot they served (median 4h34m, 2026-11-04), so one "
            "late slot is not a missing one. The gate has also missed about half its slots -- "
            "twelve expected in three days, six observed. Nine hours sits between the slowest gap "
            "that was merely late (8h49m) and the fastest gap that really missed a slot (9h59m) in "
            "the five most recent gaps.\n\n"
            "Closes when a scheduled run appears again."
        ),
        episode=gap_id,
    )


# meiva:note
# A recorded no-run gap ends at the first verified scheduled arrival. Otherwise
# the scheduled timestamp must advance strictly: older or unverified history
# cannot clear a card. An unparseable legacy gap remains visibly waiting.
def _scheduled_advanced(latest: datetime | None, previous_gap: str) -> bool:
    if latest is None:
        return False
    if previous_gap == _NO_RUN:
        return True
    try:
        previous = datetime.fromisoformat(previous_gap)
    except ValueError:
        return False
    return previous.tzinfo is not None and latest > previous


def decide_slot(
    facts: SlotFacts,
    *,
    now: datetime,
    since: datetime | None,
    stale_after: timedelta,
    open_gap: str | None = None,
) -> SlotDecision:
    gap_id = _gap_id(facts.last_schedule_start)
    if open_gap is not None:
        if _scheduled_advanced(facts.last_schedule_start, open_gap):
            return SlotDecision(
                SlotAction.RECOVERED,
                None,
                gap_id,
                f"a newer scheduled release-gate run appeared at {gap_id}",
            )
        return SlotDecision(
            SlotAction.WAIT,
            None,
            open_gap,
            "release-gate gap remains open; waiting for a verifiable newer scheduled start",
        )
    if facts.busy:
        return SlotDecision(
            SlotAction.WAIT, None, gap_id, "release-gate is running; the slot check waits"
        )
    if facts.last_schedule_start is None:
        if since is None or now - since < stale_after:
            return SlotDecision(SlotAction.WAIT, None, _NO_GAP, None)
        age = now - since
        gap_id = _NO_RUN
    else:
        age = now - facts.last_schedule_start
        if age < stale_after:
            return SlotDecision(SlotAction.WAIT, None, gap_id, None)
    if facts.uncertified_commits == 0:
        return SlotDecision(
            SlotAction.WAIT,
            None,
            gap_id,
            f"release-gate slot {age.total_seconds() / 3600:.1f}h late, nothing uncertified",
        )
    return SlotDecision(
        SlotAction.OPEN,
        _card(facts, threshold=stale_after, gap_id=gap_id),
        gap_id,
        f"release-gate slot missing: newest scheduled run {age.total_seconds() / 3600:.1f}h ago",
    )
