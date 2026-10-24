# meiva:note
# Whether the weekly self-evolution trigger fires, from the agent-event volume it counted.
#
# The main check acts only when the past week reaches `MIN_WEEKLY_EVENTS` (about 4K events a
# day: below it the week is too quiet for meaningful analysis). A main check at or above
# `HIGH_WEEKLY_EVENTS` also arms the mid-week follow-up, whose own bar is half that high
# threshold, counted from the follow-up window rather than from the whole week.
# meiva:link schedules/docs/self-evolution.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "FOLLOW_UP_WINDOW_DAYS",
    "HIGH_WEEKLY_EVENTS",
    "MIN_WEEKLY_EVENTS",
    "WEEK_DAYS",
    "WeeklyDecision",
    "decide_follow_up",
    "decide_main",
]

MIN_WEEKLY_EVENTS = 30000
HIGH_WEEKLY_EVENTS = 200000
WEEK_DAYS = 7
FOLLOW_UP_WINDOW_DAYS = 4


# meiva:note
# `prompt` is what to send the self-evolution agent (`None` when nothing fires) and
# `follow_up_next` whether the next follow-up slot should fire its check.
@dataclass(frozen=True)
class WeeklyDecision:
    log_lines: tuple[str, ...]
    prompt: str | None
    follow_up_next: bool


def decide_main(total: int, now: datetime, prompt: str) -> WeeklyDecision:
    stamp = now.isoformat()
    check = (
        f"[{stamp}] self-evolution check: {total} events in past {WEEK_DAYS} days "
        f"(min={MIN_WEEKLY_EVENTS}, high={HIGH_WEEKLY_EVENTS})"
    )
    if total < MIN_WEEKLY_EVENTS:
        skipped = f"[{stamp}] self-evolution skipped: {total} < {MIN_WEEKLY_EVENTS} minimum"
        return WeeklyDecision(log_lines=(check, skipped), prompt=None, follow_up_next=False)
    if total >= HIGH_WEEKLY_EVENTS:
        armed = (
            f"[{stamp}] self-evolution: mid-week follow-up enabled "
            f"({total} >= {HIGH_WEEKLY_EVENTS})"
        )
        return WeeklyDecision(log_lines=(check, armed), prompt=prompt, follow_up_next=True)
    return WeeklyDecision(log_lines=(check,), prompt=prompt, follow_up_next=False)


def decide_follow_up(count: int, now: datetime, prompt: str) -> WeeklyDecision:
    bar = HIGH_WEEKLY_EVENTS // 2
    check = (
        f"[{now.isoformat()}] self-evolution follow-up check: {count} events in the last "
        f"{FOLLOW_UP_WINDOW_DAYS} days (bar={bar})"
    )
    fires = count >= bar
    return WeeklyDecision(
        log_lines=(check,),
        prompt=f"{prompt} (mid-week follow-up)" if fires else None,
        follow_up_next=False,
    )
