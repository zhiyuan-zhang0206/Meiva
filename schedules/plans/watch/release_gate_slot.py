# meiva:note
# The six-hourly `release-gate` is the only post-merge run that covers the whole population on a
# fixed cadence, and GitHub does not keep that cadence here: in the three days to 2026-11-04 it
# should have run twelve times and ran six (11-01 17:56Z and 22:05Z, 11-02 06:14Z and 16:13Z,
# 11-03 01:02Z and 14:23Z), the latest slot starting 1h48m late. A slot that never arrives is not a
# red the watch can read -- there is nothing there -- so it needs its own check.
#
# The check reports; it never publishes. `release-gate.yml` certifies on success, which mints an
# immutable `v*` tag, moves `channel/latest` and creates a GitHub Release, and a release is the
# user's decision (2026-10-28). Letting "the cron drifted" trigger that would hand the release
# decision to the drift. So the round opens a card whose first line says it publishes nothing, and
# the run that closes the gap stays a human action.
#
# The gap the card names is "no full-population check for nine hours", not "no release for nine
# hours". Those are different debts, and only the first one is the watch's to report: a type-only
# pass (`main-typecheck.yml`) pays the first in minutes without touching the second, so the card
# tells the reader to run that one and says plainly what it does not restore.
#
# Two silent slots look alike and are not: the cron may not have run, or it may have run and
# skipped (`release-gate.yml` certifies only when `channel/latest` is behind `main`). The second is
# harmless, so the round also reads how many commits are uncertified and opens no card when the
# answer is none.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from schedules.plans.watch.main_red import Card

__all__ = [
    "RELEASE_GATE_SLOT_STALE_AFTER",
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
# that workflow is still running. `busy` matters because a run in flight is about to say something;
# dispatching over it would stack two full suites on one runner pool.
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


# meiva:note
# `gap_id` names the gap a make-up dispatch already answered, so one missing slot produces one
# dispatch however many rounds notice it. An empty `gap_id` means there is no gap to name.
@dataclass(frozen=True)
class SlotDecision:
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
def _arrivals(facts: SlotFacts, now: datetime) -> tuple[int, int]:
    window_start = now - RELEASE_GATE_SLOT_LOOKBACK
    observed = sum(1 for started in facts.scheduled_starts if started >= window_start)
    return observed, int(RELEASE_GATE_SLOT_LOOKBACK / facts.cadence)


def _card(facts: SlotFacts, age: timedelta, *, threshold: timedelta, now: datetime) -> Card:
    newest = (
        facts.last_schedule_start.isoformat()
        if facts.last_schedule_start is not None
        else "no scheduled run on record"
    )
    hours = age.total_seconds() / 3600
    observed, expected = _arrivals(facts, now)
    return Card(
        fingerprint=FINGERPRINT,
        title=f"release-gate has not run on its schedule for {hours:.1f}h",
        body=(
            "**This card publishes nothing.** No tag is minted, `channel/latest` does not move, "
            "and no release is created until a human runs the gate.\n\n"
            f"{_behind(facts)}\n\n"
            f"The newest scheduled `release-gate` run started at {newest}; that is {hours:.1f}h ago "
            f"(threshold {threshold.total_seconds() / 3600:.0f}h).\n\n"
            "This reports the schedule, not the code: `main` has had no full-population check in "
            "that window, so a red merged in it is invisible rather than absent.\n\n"
            f"In the {RELEASE_GATE_SLOT_LOOKBACK.total_seconds() / 3600:.0f}h to "
            f"{now.isoformat()}, this {facts.cadence.total_seconds() / 3600:.0f}-hourly cron should "
            f"have started {expected} run(s) and started {observed}. Confirm that count against the "
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
    )


# meiva:note
# The decision a round makes about one workflow's slot. Order matters: a running workflow is never
# a missing slot, a slot inside the late band is not late yet, and a slot that would have skipped
# anyway (nothing uncertified) is not worth a reader's attention. Only past all three does the
# round name a gap.
def decide_slot(
    facts: SlotFacts,
    *,
    now: datetime,
    since: datetime | None,
    stale_after: timedelta,
) -> SlotDecision:
    gap_id = _gap_id(facts.last_schedule_start)
    if facts.busy:
        return SlotDecision(
            None, gap_id=gap_id, note="release-gate is running; the slot check waits"
        )
    if facts.last_schedule_start is None:
        if since is not None and now - since < stale_after:
            return SlotDecision(None, gap_id=_NO_GAP, note=None)
        age = now - since if since is not None else stale_after
        gap_id = _NO_RUN
    else:
        age = now - facts.last_schedule_start
        if age < stale_after:
            return SlotDecision(None, gap_id=gap_id, note=None)
    if facts.uncertified_commits == 0:
        return SlotDecision(
            None,
            gap_id=gap_id,
            note=f"release-gate slot {age.total_seconds() / 3600:.1f}h late, nothing uncertified",
        )
    return SlotDecision(
        card=_card(facts, age, threshold=stale_after, now=now),
        gap_id=gap_id,
        note=f"release-gate slot missing: newest scheduled run {age.total_seconds() / 3600:.1f}h ago",
    )
