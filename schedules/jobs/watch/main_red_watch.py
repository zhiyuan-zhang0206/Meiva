# meiva:note
# The effect shell of the main-red watch: read the newest post-merge runs, hand their failures to
# `schedules/plans/main_red.py`, then do what the plan says -- open one card per confirmed red,
# close the cards whose red ended, and leave a heartbeat every round.
#
# Nothing here decides whether a red is real; that is the plan's judgement. This file owns the two
# things the plan cannot know: which runs were already read (a watermark per workflow, so a
# thirty-minute cadence does not re-download logs), and what a card is made of on this cluster (a
# task owned by the P0 lead).
#
# A round reads several streams and each read can fail on its own: a renamed or disabled workflow
# answers 404, a busy API answers 502. One unreadable stream becomes one named note and the round
# carries on, because raising would take the streams that did read down with it -- and, once the
# exception leaves the loop, the process that would have reported the failure next round as well.
# meiva:link schedules/README.md

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from base.host.boundary.json_values import is_object_dict
from schedules.plans.watch.main_red import (
    Card,
    Entry,
    RunFacts,
    apply_run,
    card_body,
    card_title,
    liveness_alert,
)
from schedules.plans.watch.release_gate_slot import (
    RELEASE_GATE_SLOT_STALE_AFTER,
    SlotFacts,
    decide_slot,
)

__all__ = [
    "STATE_FILE",
    "LivenessStream",
    "SlotLedger",
    "WatchState",
    "load_state",
    "round_line",
    "save_state",
    "slot_round",
    "watch_once",
]

STATE_FILE = "main-red-watch.json"


# meiva:note
# One workflow whose silence has to be reported: the label its note uses, and how long it may stay
# quiet before that silence means the schedule stopped.
class LivenessStream(NamedTuple):
    label: str
    workflow: str
    stale_after: timedelta


# meiva:note
# The state file's three maps, kept together so a caller cannot pass one where another belongs: the
# entries of the reds being tracked, the newest run read per workflow, and when this watch first
# saw each workflow at all.
# meiva:note
# What the slot check remembers between rounds: the gap its card belongs to and that card's id, so
# one missing slot stays one card however many rounds notice it.
@dataclass(frozen=True)
class SlotLedger:
    gap_id: str | None = None
    task_id: int | None = None


@dataclass(frozen=True)
class WatchState:
    entries: Mapping[str, Entry]
    watermarks: Mapping[str, str]
    first_seen: Mapping[str, str]
    slot: SlotLedger = SlotLedger()


# meiva:note
# The slot ledger is a boundary read: the file may predate it, or carry a value a hand edit broke.
# Each field is accepted only in its own type, so a malformed ledger costs one round's memory of
# the gap rather than the whole state file.
def _load_slot(raw: object) -> SlotLedger:
    if not is_object_dict(raw):
        return SlotLedger()
    gap = raw.get("gap_id")
    task = raw.get("task_id")
    return SlotLedger(
        gap_id=gap if isinstance(gap, str) else None,
        task_id=task if isinstance(task, int) and not isinstance(task, bool) else None,
    )


def load_state(path: Path) -> WatchState:
    if not path.exists():
        return WatchState({}, {}, {})
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = {
        name: Entry(
            fingerprint=name,
            state=value["state"],
            first_seen_sha=value["first_seen_sha"],
            first_seen_at=datetime.fromisoformat(value["first_seen_at"]),
            last_red_sha=value["last_red_sha"],
            last_red_at=datetime.fromisoformat(value["last_red_at"]),
            red_count=int(value["red_count"]),
            last_green_sha=value.get("last_green_sha"),
            last_green_at=(
                datetime.fromisoformat(value["last_green_at"])
                if value.get("last_green_at")
                else None
            ),
            task_id=value.get("task_id"),
        )
        for name, value in raw.get("entries", {}).items()
    }
    slot = _load_slot(raw.get("slot"))
    return WatchState(
        entries,
        dict(raw.get("watermarks", {})),
        dict(raw.get("first_seen", {})),
        slot,
    )


def save_state(path: Path, state: WatchState) -> None:
    payload = {
        "entries": {
            name: {
                "state": entry.state,
                "first_seen_sha": entry.first_seen_sha,
                "first_seen_at": entry.first_seen_at.isoformat(),
                "last_red_sha": entry.last_red_sha,
                "last_red_at": entry.last_red_at.isoformat(),
                "red_count": entry.red_count,
                "last_green_sha": entry.last_green_sha,
                "last_green_at": entry.last_green_at.isoformat() if entry.last_green_at else None,
                "task_id": entry.task_id,
            }
            for name, entry in state.entries.items()
        },
        "watermarks": dict(state.watermarks),
        "first_seen": dict(state.first_seen),
        "slot": {
            "gap_id": state.slot.gap_id,
            "task_id": state.slot.task_id,
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


# meiva:note
# The commit a card points at, subject first: a reader of the card has to know which merge to look
# at, and a sha alone sends them to `git log` before they can tell whether it is theirs.
def _commit_line(run: RunFacts) -> tuple[str, ...]:
    if not run.head_sha:
        return ()
    short = run.head_sha[:10]
    return (f"{short} {run.head_title}" if run.head_title else short,)


def round_line(*, entries: int, confirmed: int, infra: int, notes: tuple[str, ...]) -> str:
    head = f"fingerprints={entries} confirmed={confirmed} infra={infra}"
    return head if not notes else f"{head} | " + " | ".join(notes)


def watch_once(
    *,
    state_path: Path,
    now: datetime,
    fetch: Callable[[str], tuple[RunFacts, ...]],
    last_start: Callable[[str], datetime | None],
    open_card: Callable[[Card], int],
    close_card: Callable[[int, str], None],
    emit: Callable[[Mapping[str, object]], None],
    workflows: tuple[str, ...],
    liveness: tuple[LivenessStream, ...],
) -> str:
    state = load_state(state_path)
    entries = dict(state.entries)
    watermarks = dict(state.watermarks)
    first_seen = dict(state.first_seen)
    for workflow in (*workflows, *(stream.workflow for stream in liveness)):
        first_seen.setdefault(workflow, now.isoformat())
    notes: list[str] = []
    stale = 0
    opened = 0
    infra = 0
    for workflow in workflows:
        watermark = watermarks.get(workflow)
        seen_at = datetime.fromisoformat(watermark) if watermark else None
        try:
            runs = fetch(workflow)
        except Exception as exc:
            notes.append(f"stream {workflow} unreadable: {type(exc).__name__}: {exc}")
            stale += 1
            continue
        newest = seen_at
        for run in reversed(runs):
            if seen_at is not None and run.created_at <= seen_at:
                continue
            newest = run.created_at if newest is None else max(newest, run.created_at)
            plan = apply_run(entries, run)
            entries = dict(plan.entries)
            infra += sum(1 for note in plan.notes if "infra on" in note)
            for entry in plan.confirmed:
                card = Card(
                    entry.fingerprint,
                    card_title(entry),
                    card_body(
                        entry,
                        latest_url=run.html_url,
                        job_names=run.job_names,
                        candidate_commits=_commit_line(run),
                    ),
                )
                task_id = open_card(card)
                entries[entry.fingerprint] = replace(entries[entry.fingerprint], task_id=task_id)
                opened += 1
                notes.append(f"opened card {task_id} for {entry.fingerprint}")
            for task_id in plan.closes:
                close_card(task_id, "resolved by a green run")
                notes.append(f"closed card {task_id}")
        if newest is not None:
            watermarks[workflow] = newest.isoformat()
    for stream in liveness:
        try:
            started = last_start(stream.workflow)
        except Exception as exc:
            notes.append(f"stream {stream.workflow} unreadable: {type(exc).__name__}: {exc}")
            stale += 1
            continue
        alert = liveness_alert(
            stream.label,
            started,
            now,
            stale_after=stream.stale_after,
            since=datetime.fromisoformat(first_seen[stream.workflow]),
        )
        if alert is not None:
            notes.append(alert)
            stale += 1
    save_state(state_path, WatchState(entries, watermarks, first_seen))
    emit(
        {
            "fingerprints": len(entries),
            "confirmed": opened,
            "infra": infra,
            "stale": stale,
        }
    )
    return round_line(entries=len(entries), confirmed=opened, infra=infra, notes=tuple(notes))


# meiva:note
# The slot round: one workflow's cron can stop without anything going red, and the watch would
# otherwise be silent through exactly the outage it exists to end. A missing slot opens one card
# and the next scheduled run closes it. The round dispatches nothing: the make-up run certifies,
# certifying releases, and a release is the user's decision -- so the card carries the command and
# stays open until a human runs it or the cron recovers.
def slot_round(
    *,
    state_path: Path,
    now: datetime,
    facts: SlotFacts,
    open_card: Callable[[Card], int],
    close_card: Callable[[int, str], None],
    since: datetime | None = None,
) -> str | None:
    state = load_state(state_path)
    ledger = state.slot
    decision = decide_slot(
        facts,
        now=now,
        since=since,
        stale_after=RELEASE_GATE_SLOT_STALE_AFTER,
    )
    if decision.card is None:
        if ledger.task_id is not None:
            close_card(ledger.task_id, decision.note or "a scheduled release-gate run appeared")
        slot = SlotLedger()
    else:
        task_id = ledger.task_id if ledger.task_id is not None else open_card(decision.card)
        slot = SlotLedger(gap_id=decision.gap_id, task_id=task_id)
    save_state(state_path, replace_state_slot(state, slot))
    return decision.note


def replace_state_slot(state: WatchState, slot: SlotLedger) -> WatchState:
    return WatchState(state.entries, state.watermarks, state.first_seen, slot)
