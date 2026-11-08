# meiva:note
# What the watch says when its own streams stop being readable. A round that cannot read a stream
# has always reported it as one named note and carried on, and that is the right shape for a round:
# raising would take the streams that did read down with it. It is the wrong shape for a run of
# rounds. On 2026-11-08 four streams failed the same way within one round and the watch opened
# nothing, said nothing outside its own log line, and looked from the card list exactly like a watch
# whose `main` was clean -- the blindness was found because someone asked, not because the
# instrument called. A card list that cannot tell "nothing is red" from "nothing is being read" is
# the failure this module ends.
#
# Two triggers, and the second is not the first with a bigger number. Two or more streams unreadable
# in one round is a round that judged almost nothing. One stream unreadable for many rounds is a
# watch that looks healthy -- the other streams keep reporting and the heartbeat keeps landing --
# while one judgement is missing; when that one is the full suite, every card the round opens is
# describing a `main` nobody checked. The first trigger is about a round, the second about a stream.
#
# The card is one per window, not one per round: `episode` is when the window opened, so thirty
# rounds of blindness answer to one create key and a later window is a card of its own.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from schedules.plans.watch.main_red import Card

__all__ = [
    "BLIND_STREAMS",
    "BLIND_STREAM_ROUNDS",
    "BlindWindow",
    "blind_card",
    "blind_close_line",
    "blind_due",
]

# meiva:note
# How many streams have to fail within one round before the round says the judgement itself stopped.
# One is a noisy API or a renamed workflow, and the round still judged the rest; two or more means a
# large part of `main` went unread, and the cards the round did open cannot be read as the whole
# story. The value is a parameter of `advance_blindness` as well, so a deployment can move it without
# touching this judgement.
BLIND_STREAMS = 2

# meiva:note
# How many consecutive rounds one stream may stay unreadable before it is reported on its own. Six
# rounds is three hours at the half-hourly cadence: long enough that one blip is not a card, short
# enough that a stream cannot rot through a night. A card per round would train everyone to ignore
# the title; a longer window would let the full-suite stream go blind for a working day while the
# other three kept reporting a healthy-looking `main`.
BLIND_STREAM_ROUNDS = 6


# meiva:note
# One window of blindness, as the card states it. `started_at` is the first round the streams it
# names were unreadable, not the round the card opened: a stream that has failed for six rounds has
# been unjudged for three hours by the time it is worth a card, and a window that says otherwise
# would understate exactly the interval a reader has to go back over.
@dataclass(frozen=True)
class BlindWindow:
    started_at: datetime
    streams: tuple[tuple[str, str], ...]
    rounds: int
    total_streams: int
    reason: str


# meiva:note
# Why this round is worth a card, in the words the card carries, or None while the round is still
# inside what a stream is allowed to do. The total is passed in rather than counted from
# `unreadable`, because "3 of 4" is the sentence a reader needs and the unreadable map only knows
# the three.
def blind_due(
    unreadable: Sequence[str],
    streaks: Mapping[str, int],
    *,
    total_streams: int,
    min_streams: int,
    stream_rounds: int,
) -> str | None:
    if len(unreadable) >= min_streams:
        return f"{len(unreadable)} of {total_streams} streams unreadable in one round"
    for workflow in unreadable:
        rounds = streaks.get(workflow, 0)
        if rounds >= stream_rounds:
            return f"`{workflow}` unreadable for {rounds} consecutive rounds"
    return None


# meiva:note
# The card, written once when the window opens. Its three sections answer three different questions:
# how long this has been going on and when it ends, which judgement is missing and why, and what a
# reader may not conclude from the cards that are absent. The last one is the point -- a blind watch
# and a clean `main` produce the same empty list, and only the card can tell them apart.
def blind_card(window: BlindWindow) -> Card:
    named = len(window.streams)
    streams = "\n".join(f"- `{workflow}` — `{error}`" for workflow, error in window.streams)
    if named == 1:
        title = (
            f"main-red watch blind: `{window.streams[0][0]}` unreadable for {window.rounds} rounds"
        )
    else:
        title = (
            f"main-red watch blind: {named} of {window.total_streams} streams unreadable since "
            f"{window.started_at.isoformat()}"
        )
    return Card(
        fingerprint="main-red-watch/blindness",
        title=title,
        body=(
            f"The watch could not read {named} of its {window.total_streams} streams, so it "
            f"stopped judging `main` on the round at {window.started_at.isoformat()}.\n\n"
            "**Window** — from that round until every stream reads again. This card closes on that "
            "round, and its closing line records how long the window lasted and whether the runs "
            "that landed inside it were re-read.\n\n"
            f"**Streams**\n{streams}\n\n"
            "**What this means** — while the window is open a red on `main` opens no card, so "
            "anything merged inside it is **unjudged, not green**. Recovery resumes from each "
            "stream's own watermark, which is where the missed runs are read from; where one "
            "listing cannot reach back that far, the closing line says the interval was **not** "
            "re-read instead of claiming it was.\n\n"
            f"**Why now** — {window.reason}."
        ),
        episode=window.started_at.isoformat(),
    )


# meiva:note
# What the round that ends the window says when it closes the card. The interval is the part that
# cannot be known while the card is open, and the backfill verdict is the part a later reader would
# otherwise have to reconstruct: `covered` means the runs that landed inside the window were read
# after it, `NOT covered` means the listing could not reach back and those runs are unknown rather
# than green. A stream with no verdict is written as such -- an assumption is not a measurement.
def blind_close_line(
    window_start: datetime,
    ended_at: datetime,
    *,
    rounds: int,
    streams: Sequence[str],
    backfilled: Mapping[str, bool],
) -> str:
    verdicts = ", ".join(
        f"{workflow} {'covered' if backfilled.get(workflow) else 'NOT covered'}"
        for workflow in streams
    )
    named = ", ".join(streams)
    return (
        f"blind window {window_start.isoformat()} -> {ended_at.isoformat()} "
        f"({rounds} round(s); streams: {named}). "
        f"Reds that landed inside it were not judged. Backfill: {verdicts or 'no stream recorded'}."
    )
