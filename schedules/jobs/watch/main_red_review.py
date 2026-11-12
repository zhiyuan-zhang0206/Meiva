# meiva:note
# A later workflow run is an opportunity; only its own JUnit says whether this
# case ran. The discovery card records the first opportunity, including a green
# case inside a failed workflow. Later actual case greens can retire that card.
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime

from schedules.plans.watch.junit_cases import CaseObservation, CaseOutcome
from schedules.plans.watch.main_red import (
    Card,
    Entry,
    RunFacts,
    STATE_SUSPECTED,
    STATE_CONFIRMED,
    card_body,
    card_title,
)

CaseReader = Callable[[Entry, RunFacts], CaseObservation]


def review_run(
    previous: Mapping[str, Entry],
    entries: dict[str, Entry],
    run: RunFacts,
    read_case: CaseReader,
    open_card: Callable[[Card], int],
    close_card: Callable[[int, str], None],
    notes: list[str],
    *,
    charge: bool = True,
) -> None:
    for name, old in previous.items():
        current = entries.get(name)
        if (
            (old.state != STATE_SUSPECTED and not old.review_run_id)
            or current is None
            or (current.state != STATE_SUSPECTED and not old.review_run_id)
            or old.workflow != run.workflow
            or run.created_at <= old.first_seen_at
        ):
            continue
        if any(failure.test_id == name.partition("::")[2] for failure in run.failures):
            continue
        try:
            observation = read_case(old, run)
        except Exception as exc:
            observation = CaseObservation(CaseOutcome.UNREADABLE, f"{type(exc).__name__}: {exc}")
        updated = replace(
            current,
            opportunities=current.opportunities
            + int(charge and current.review_run_id != run.run_id),
            review_run_id=run.run_id,
            review_outcome=observation.outcome,
            review_detail=observation.detail,
        )
        if old.task_id is not None and observation.outcome == CaseOutcome.PASSED:
            close_card(
                old.task_id,
                f"case passed on {run.head_sha}, run {run.run_id}: {observation.detail}",
            )
            del entries[name]
            notes.append(f"closed card {old.task_id}: exact case passed in run {run.run_id}")
            continue
        if observation.outcome == CaseOutcome.FAILED:
            updated = replace(
                updated,
                state=STATE_CONFIRMED,
                red_count=old.red_count + int(old.run_id != run.run_id),
                last_red_sha=run.head_sha,
                last_red_at=run.created_at,
                run_id=run.run_id,
            )
            if old.task_id is None:
                task_id = open_card(
                    Card(
                        name,
                        card_title(updated),
                        card_body(
                            updated,
                            latest_url=run.html_url,
                            job_names=run.job_names,
                            candidate_commits=(run.head_sha,),
                        ),
                        old.first_seen_at.isoformat(),
                    )
                )
                updated = replace(updated, task_id=task_id)
            entries[name] = updated
            continue
        if old.task_id is None:
            task_id = open_card(_review_card(updated, observation, run))
            updated = replace(updated, task_id=task_id)
            notes.append(f"opened re-check card {task_id} for {name}: {observation.outcome.value}")
        entries[name] = updated


_SUSPECT_LIMIT = 10
_SUSPECT_NOTE_LIMIT = 3


# meiva:note
# The suspects, oldest first, as rows the round event carries. Oldest first because the age of a
# suspect is the whole question: one seen a week ago with no chance to re-check is the case that
# never reaches anyone (task #6488).
def suspect_rows(
    entries: Mapping[str, Entry], *, now: datetime | None = None, limit: int = _SUSPECT_LIMIT
) -> tuple[list[dict[str, object]], int]:
    rows: list[dict[str, object]] = [
        {
            "fingerprint": entry.fingerprint,
            "first_seen_at": entry.first_seen_at.isoformat(),
            "red_count": entry.red_count,
            "age_seconds": max(0, (now - entry.first_seen_at).total_seconds()) if now else 0,
            "opportunities": entry.opportunities,
            "review_run_id": entry.review_run_id,
            "review_outcome": entry.review_outcome.value
            if entry.review_outcome
            else "no-opportunity",
            "review_detail": entry.review_detail,
            "task_id": entry.task_id,
        }
        for entry in entries.values()
        if entry.state == STATE_SUSPECTED
    ]
    rows.sort(key=lambda row: str(row["first_seen_at"]))
    return rows[:limit], max(0, len(rows) - limit)


# meiva:note
# The one thing about a suspect that the round says out loud, and it is not its age: an age with no
# chance to re-check says something about `main`, not about the red. A suspect with zero
# opportunities has waited through no run at all, so nothing will ever confirm or retire it -- that
# is the shape with no exit, and it is the only shape worth a line every round (405, 2026-11-08).
def no_opportunity_notes(
    rows: Sequence[Mapping[str, object]], *, limit: int = _SUSPECT_NOTE_LIMIT
) -> tuple[str, ...]:
    waiting = [row for row in rows if row.get("opportunities") == 0]
    lines = [
        f"suspect {row['fingerprint']} has had no re-check opportunity since {row['first_seen_at']}"
        for row in waiting[:limit]
    ]
    if len(waiting) > limit:
        lines.append(f"{len(waiting) - limit} more suspects have had no re-check opportunity")
    return tuple(lines)


# meiva:note
# The one-shot notice 405 asked for: the round that gives this watch its first suspect says so,
# once. It is a transition, not a state -- every round after it has a suspect already and stays
# quiet -- so the message marks the moment the question became real instead of being a promise that
# somebody will remember to look (405, 2026-11-08).
def first_suspect(previous: Mapping[str, Entry], current: Mapping[str, Entry]) -> Entry | None:
    if any(entry.state == STATE_SUSPECTED for entry in previous.values()):
        return None
    fresh = [entry for entry in current.values() if entry.state == STATE_SUSPECTED]
    if not fresh:
        return None
    return min(fresh, key=lambda entry: entry.first_seen_at)


# meiva:note
# Phase-one ledgers may already have an opportunity but no case evidence. Read
# metadata once for that stream and backfill JUnit without replaying its failed
# logs or charging those historical runs again. Listing coverage remains bounded.
def backfill_reviews(
    entries: dict[str, Entry],
    workflow: str,
    history: Callable[[str], tuple[RunFacts, ...]],
    read_case: CaseReader,
    open_card: Callable[[Card], int],
    close_card: Callable[[int, str], None],
    notes: list[str],
    *,
    seen_through: datetime | None = None,
) -> None:
    pending = {
        name: entry
        for name, entry in entries.items()
        if entry.workflow == workflow
        and entry.state == STATE_SUSPECTED
        and entry.opportunities > 0
        and entry.review_run_id == 0
    }
    if not pending:
        return
    try:
        runs = history(workflow)
    except Exception as exc:
        _unreadable_backlog(
            entries,
            pending,
            f"history for {workflow} unreadable: {type(exc).__name__}: {exc}",
            open_card,
            notes,
        )
        return
    for run in sorted(runs, key=lambda run: (run.created_at, run.attempt)):
        if seen_through is not None and run.created_at > seen_through:
            continue
        current = {name: entries[name] for name in pending if name in entries}
        review_run(current, entries, run, read_case, open_card, close_card, notes, charge=False)
    _unreadable_backlog(
        entries,
        pending,
        "no later reviewed run in bounded history; retained opportunity count has no recoverable case evidence",
        open_card,
        notes,
    )


def _review_card(entry: Entry, observation: CaseObservation, run: RunFacts | None) -> Card:
    reviewed = f"Run {run.run_id}, SHA `{run.head_sha}`" if run else "Bounded run history"
    body = (
        f"Fingerprint: `{entry.fingerprint}`\n\nFirst seen: {entry.first_seen_at.isoformat()} "
        f"on `{entry.first_seen_sha}` (run {entry.run_id}).\n\n"
        f"Same-workflow completed re-check opportunities: {entry.opportunities}. "
        "The result below describes this reviewed evidence; the count can include earlier historical opportunities.\n\n"
        f"{reviewed}: **{observation.outcome.value}**. {observation.detail}\n\n"
        "A completed workflow is not proof that this case ran. This card records the single red "
        "and its re-check; a normal second red uses this same card. "
        "Close only on a later exact case green or an explicit decision."
    )
    return Card(
        entry.fingerprint,
        f"Single main red re-check: {entry.fingerprint}",
        body,
        f"suspect-review:{entry.first_seen_sha}:{entry.first_seen_at.isoformat()}",
    )


def _unreadable_backlog(
    entries: dict[str, Entry],
    pending: Mapping[str, Entry],
    reason: str,
    open_card: Callable[[Card], int],
    notes: list[str],
) -> None:
    for name in pending:
        current = entries.get(name)
        if current is None or current.review_run_id:
            continue
        observation = CaseObservation(CaseOutcome.UNREADABLE, reason)
        task_id = current.task_id or open_card(_review_card(current, observation, None))
        entries[name] = replace(
            current, task_id=task_id, review_outcome=observation.outcome, review_detail=reason
        )
        notes.append(f"suspect re-check card {task_id}: {reason}")
