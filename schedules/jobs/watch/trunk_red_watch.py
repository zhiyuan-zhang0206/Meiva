# meiva:note
# The effect shell of the trunk-red watch: read the newest post-merge runs, hand their failures to
# `schedules/plans/trunk_red.py`, then do what the plan says -- open one card per confirmed red,
# close the cards whose red ended, and leave a heartbeat every round.
#
# Nothing here decides whether a red is real; that is the plan's judgement. This file owns the two
# things the plan cannot know: which runs were already read (a watermark per workflow, so a
# thirty-minute cadence does not re-download logs), and what a card is made of on this cluster (a
# task owned by the P0 lead).
# meiva:link schedules/README.md

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from schedules.plans.watch.trunk_red import (
    Card,
    Entry,
    RunFacts,
    apply_run,
    card_body,
    card_title,
    liveness_alert,
    NIGHTLY_STALE_AFTER,
    RELEASE_GATE_STALE_AFTER,
)

__all__ = ["STATE_FILE", "load_state", "round_line", "save_state", "watch_once"]

STATE_FILE = "trunk-red-watch.json"


def load_state(path: Path) -> tuple[dict[str, Entry], dict[str, str]]:
    if not path.exists():
        return {}, {}
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
    return entries, dict(raw.get("watermarks", {}))


def save_state(path: Path, entries: Mapping[str, Entry], watermarks: Mapping[str, str]) -> None:
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
            for name, entry in entries.items()
        },
        "watermarks": dict(watermarks),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def round_line(*, entries: int, confirmed: int, infra: int, notes: tuple[str, ...]) -> str:
    head = f"fingerprints={entries} confirmed={confirmed} infra={infra}"
    return head if not notes else f"{head} | " + " | ".join(notes)


def watch_once(
    *,
    state_path: Path,
    now: datetime,
    fetch: Callable[[str], tuple[RunFacts, ...]],
    open_card: Callable[[Card], int],
    close_card: Callable[[int, str], None],
    emit: Callable[[Mapping[str, object]], None],
    workflows: tuple[str, ...],
    release_gate_last_start: datetime | None,
    nightly_last_start: datetime | None,
) -> str:
    entries, watermarks = load_state(state_path)
    notes: list[str] = []
    opened = 0
    infra = 0
    for workflow in workflows:
        watermark = watermarks.get(workflow)
        seen_at = datetime.fromisoformat(watermark) if watermark else None
        newest = seen_at
        for run in reversed(fetch(workflow)):
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
                        candidate_commits=(),
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
    for label, last_start, stale_after in (
        ("release-gate", release_gate_last_start, RELEASE_GATE_STALE_AFTER),
        ("nightly", nightly_last_start, NIGHTLY_STALE_AFTER),
    ):
        alert = liveness_alert(label, last_start, now, stale_after=stale_after)
        if alert is not None:
            notes.append(alert)
    save_state(state_path, entries, watermarks)
    emit(
        {
            "fingerprints": len(entries),
            "confirmed": opened,
            "infra": infra,
            "stale": sum(1 for note in notes if "no new run" in note or "never started" in note),
        }
    )
    return round_line(entries=len(entries), confirmed=opened, infra=infra, notes=tuple(notes))
