# meiva:note
# The slot round: one workflow's cron can stop without anything going red, and the watch would
# otherwise be silent through exactly the outage it exists to end. A missing slot opens one card
# and the next scheduled run closes it. The round dispatches nothing: the make-up run certifies,
# certifying releases, and a release is the user's decision -- so the card carries the command and
# stays open until a human runs it or the cron recovers.
#
# It lives beside `main_red_watch.py` rather than inside it because the two judge different things
# over one state file: one reads runs, the other watches a schedule, and the shared file is all they
# have in common. Splitting them also keeps the run-reading round inside the file budget it shares
# with every stream it reads.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from schedules.jobs.watch.main_red_watch import SlotLedger, WatchState, load_state, save_state
from schedules.plans.watch.main_red import Card
from schedules.plans.watch.release_gate_slot import (
    RELEASE_GATE_SLOT_STALE_AFTER,
    SlotFacts,
    decide_slot,
)

__all__ = ["replace_state_slot", "slot_round"]


# meiva:note
# The slot round: one workflow's cron can stop without anything going red, and the watch would
# otherwise be silent through exactly the outage it exists to end. A missing slot opens one card
# and the next scheduled run closes it. The round dispatches nothing: the make-up run certifies,
# certifying releases, and a release is the user's decision -- so the card carries the command and
# stays open until a human runs it or the cron recovers.
# meiva:note
# How often an unreadable slot leg repeats itself on the round line. One note per three rounds --
# ninety minutes at the current cadence -- is often enough that the line never goes quiet while the
# judgement is not running, and rare enough that it does not become the only thing on the line.
_UNREADABLE_NOTE_EVERY = 3


def slot_round(
    *,
    state_path: Path,
    now: datetime,
    facts: SlotFacts | None,
    open_card: Callable[[Card], int],
    close_card: Callable[[int, str], None],
    since: datetime | None = None,
    unreadable: str | None = None,
) -> str | None:
    state = load_state(state_path)
    ledger = state.slot
    if facts is None:
        rounds = ledger.unreadable_rounds + 1
        save_state(state_path, replace_state_slot(state, replace(ledger, unreadable_rounds=rounds)))
        if rounds % _UNREADABLE_NOTE_EVERY != 0:
            return None
        return (
            f"release-gate slot has not been judged for {rounds} consecutive rounds "
            f"({unreadable or 'unreadable'}): nothing is watching the slot while this lasts"
        )
    recovered = ledger.unreadable_rounds
    decision = decide_slot(
        facts,
        now=now,
        since=since,
        stale_after=RELEASE_GATE_SLOT_STALE_AFTER,
    )
    if decision.card is None:
        if ledger.task_id is not None:
            close_card(
                ledger.task_id,
                f"release-gate slot: {decision.note or 'a scheduled release-gate run appeared'}",
            )
        slot = SlotLedger()
    else:
        task_id = ledger.task_id if ledger.task_id is not None else open_card(decision.card)
        slot = SlotLedger(gap_id=decision.gap_id, task_id=task_id)
    save_state(state_path, replace_state_slot(state, slot))
    if recovered:
        back = f"release-gate slot readable again after {recovered} unreadable round(s)"
        return back if decision.note is None else f"{back} | {decision.note}"
    return decision.note


# meiva:note
# `replace`, not a fresh `WatchState(...)`: the positional form silently dropped whichever maps were
# added after it was written, and a map that vanishes on every slot round is a memory that looks
# like it works until the round that needed it.
def replace_state_slot(state: WatchState, slot: SlotLedger) -> WatchState:
    return replace(state, slot=slot)
