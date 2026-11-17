# meiva:link schedules/docs/self-evolution.md

import os
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.jobs.self_evolution_weekly import (
    FOLLOW_UP_TRIGGER,
    MAIN_TRIGGER,
    handle_weekly_trigger,
    read_events_api,
)
from schedules.plans.schedule_identity import parse_scheduled_for

PROMPT = "Read and run $MEIVA_HOME/skills/meiva-self-evolution/SKILL.md for this week."

# meiva:note
# The main check is Tuesday 00:00 and the mid-week follow-up Friday 00:00 cluster time; the
# trigger name travels in the slot payload.
MAIN_CRON = "0 0 * * 2"
FOLLOW_UP_CRON = "0 0 * * 5"
_STATE = Path("state.json")


def _follow_up_armed() -> bool:
    try:
        value = json.loads(_STATE.read_text(encoding="utf-8")).get("follow_up_armed")
    except (OSError, ValueError):
        return False
    return value is True


def _save_follow_up(*, armed: bool) -> None:
    temporary = _STATE.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"follow_up_armed": armed}) + "\n", encoding="utf-8")
    temporary.replace(_STATE)


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        del inputs
        slot = parse_scheduled_for(os.environ.get("MEIVA_SCHEDULED_FOR"))
        weekday = slot.astimezone(ZoneInfo(cluster_timezone())).weekday()
        trigger = MAIN_TRIGGER if weekday == 1 else FOLLOW_UP_TRIGGER
        if trigger == FOLLOW_UP_TRIGGER and not _follow_up_armed():
            print(f"[{slot.isoformat()}] self-evolution follow-up not armed")
        else:
            armed = handle_weekly_trigger(
                slot,
                trigger,
                api=read_events_api(os.environ, home),
                prompt=PROMPT,
            )
            _save_follow_up(armed=armed)
