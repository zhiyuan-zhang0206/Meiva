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

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from base.host.boundary.json_values import is_object_dict
from schedules.plans.watch.main_red import (
    FAILURE_TEST,
    FAILURE_UNNAMED,
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

Compare = Callable[[str, str], tuple[str, ...]]

__all__ = [
    "ATTEMPT_GREEN",
    "ATTEMPT_HOLDS",
    "ATTEMPT_PENDING",
    "STATE_FILE",
    "AttemptOf",
    "Compare",
    "LivenessStream",
    "SlotLedger",
    "WatchState",
    "card_operation_key",
    "job_attempt_verdict",
    "listing_attempt",
    "load_state",
    "round_line",
    "save_state",
    "slot_round",
    "watch_once",
]

STATE_FILE = "main-red-watch.json"


# meiva:note
# The create key for one card: what it watches and which occurrence it reports, hashed to a fixed
# width so a long test id cannot push the key past the registry's 128-character limit. The text is
# deliberately not an input -- a card whose wording moves within one occurrence must replay instead
# of failing, and only a new occurrence may claim a new key.
def card_operation_key(card: Card) -> str:
    material = f"{card.fingerprint}\x00{card.episode}".encode()
    return f"main-red-{hashlib.sha256(material).hexdigest()[:24]}"


# meiva:note
# One workflow whose silence has to be reported: the label its note uses, how long it may stay
# quiet before that silence means the schedule stopped, and the command that closes the gap. The
# command is empty for a stream nobody has measured a dispatch of; the note then reports the
# silence and names no action rather than inventing one.
class LivenessStream(NamedTuple):
    label: str
    workflow: str
    stale_after: timedelta
    make_up: str | None = None


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
    # meiva:note
    # How many consecutive rounds could not read the slot leg at all. Without it a failing `gh`
    # looks exactly like a gate that keeps its schedule: the round stays alive, the heartbeat keeps
    # landing, and the thing that is not running is the judgement itself.
    unreadable_rounds: int = 0


# meiva:note
# `greens` is the newest successful full-suite run read per workflow, which is the base a card's
# candidate range starts from. It cannot live on an `Entry`: a green run deletes the entry it
# resolves, so the sha would be dropped at exactly the moment it becomes the one a later red needs.
@dataclass(frozen=True)
class WatchState:
    entries: Mapping[str, Entry]
    watermarks: Mapping[str, str]
    first_seen: Mapping[str, str]
    slot: SlotLedger = SlotLedger()
    greens: Mapping[str, str] = field(default_factory=dict[str, str])


# meiva:note
# The slot ledger is a boundary read: the file may predate it, or carry a value a hand edit broke.
# Each field is accepted only in its own type, so a malformed ledger costs one round's memory of
# the gap rather than the whole state file.
def _load_slot(raw: object) -> SlotLedger:
    if not is_object_dict(raw):
        return SlotLedger()
    gap = raw.get("gap_id")
    task = raw.get("task_id")
    rounds = raw.get("unreadable_rounds")
    return SlotLedger(
        gap_id=gap if isinstance(gap, str) else None,
        task_id=task if isinstance(task, int) and not isinstance(task, bool) else None,
        unreadable_rounds=(
            rounds if isinstance(rounds, int) and not isinstance(rounds, bool) and rounds > 0 else 0
        ),
    )


# meiva:note
# A watermark is "the newest `(created_at, attempt)` this watch has already read" for one workflow.
# It used to be only the timestamp, which cannot see a re-run: GitHub keeps a run's id and
# `created_at` and only advances `run_attempt`, so attempt 2 looked exactly like a run already read —
# the red it cleared stayed on the books and the red it repeated was never counted (task #6488).
# A ledger written before this encoding holds a bare ISO timestamp; it means "that run was read, at
# whatever attempt it then had", which is recorded here as an unknown attempt. That choice is
# deliberate: reading an old boundary run again would count one red twice and could confirm a card
# from a single event, while treating it as read can at most miss one attempt of one run that was
# already in flight when the encoding changed.
_WATERMARK_SEPARATOR = "|"


def _read_watermark(raw: str | None) -> tuple[datetime | None, int | None]:
    if not raw:
        return None, None
    stamp, _separator, attempt = raw.partition(_WATERMARK_SEPARATOR)
    try:
        seen_at = datetime.fromisoformat(stamp)
    except ValueError:
        return None, None
    if not attempt:
        return seen_at, None
    try:
        return seen_at, int(attempt)
    except ValueError:
        return seen_at, None


def _format_watermark(seen_at: datetime, attempt: int | None) -> str:
    if attempt is None:
        return seen_at.isoformat()
    return f"{seen_at.isoformat()}{_WATERMARK_SEPARATOR}{attempt}"


def _already_read(run: RunFacts, seen_at: datetime | None, seen_attempt: int | None) -> bool:
    if seen_at is None:
        return False
    if run.created_at < seen_at:
        return True
    if run.created_at > seen_at:
        return False
    return seen_attempt is None or run.attempt <= seen_attempt


# meiva:note
# Which attempt a listing row says it is. `run_attempt` is the only field that moves when a run is
# re-run -- the id and `created_at` do not -- so reading it wrongly is the same as not reading it at
# all, and it is read once here instead of at each call site. A row that carries no attempt (or a
# value that is not a positive integer) means the first attempt, which is what every run is.
def listing_attempt(item: Mapping[str, object]) -> int:
    raw = item.get("run_attempt")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
        return raw
    return 1


# meiva:note
# What a re-run may say about a claim. Only `success` withdraws it: `failure`, `cancelled` and a job
# that is missing from the newest attempt all leave the claim standing, because "there is a newer
# attempt" is not "the newer attempt is green" (405, 2026-11-08). An attempt that has not concluded
# settles nothing this round -- opening a card on it would report a red the re-run may still clear,
# and withdrawing the claim on it would answer "safe" from a fact nobody has yet.
ATTEMPT_GREEN = "green"
ATTEMPT_HOLDS = "holds"
ATTEMPT_PENDING = "pending"


# meiva:note
# `job_attempt_verdict` answers about one job among one attempt's jobs, and returns the verdict
# together with what it actually read -- the conclusion, or that the job was not listed. The second
# half is what lets a round line say which conclusion suppressed a card instead of only that it did.
def job_attempt_verdict(jobs: Sequence[Mapping[str, object]], job_name: str) -> tuple[str, str]:
    for job in jobs:
        if not is_object_dict(job) or job.get("name") != job_name:
            continue
        conclusion = job.get("conclusion")
        if not isinstance(conclusion, str) or not conclusion:
            return ATTEMPT_PENDING, "no conclusion yet"
        return (ATTEMPT_GREEN if conclusion == "success" else ATTEMPT_HOLDS), conclusion
    return ATTEMPT_HOLDS, "missing from that attempt"


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
            # meiva:note
            # A ledger written before the class existed carries no `kind`; those entries were all
            # named cases, which is the default, so an upgrade never re-reads an old red as new.
            kind=value.get("kind") or FAILURE_TEST,
            # meiva:note
            # Absent in a ledger written before the field: that entry keeps no job, and the attempt
            # gate skips it rather than guess which job its red belonged to.
            job=value.get("job") or "",
            # meiva:note
            # A ledger written before the field existed holds full-suite reds (the push gate's were
            # the only partial ones), so the default keeps an upgrade from re-reading an old red as
            # something only its own lane may retire.
            covers_full_suite=bool(value.get("covers_full_suite", True)),
        )
        for name, value in raw.get("entries", {}).items()
    }
    slot = _load_slot(raw.get("slot"))
    return WatchState(
        entries,
        dict(raw.get("watermarks", {})),
        dict(raw.get("first_seen", {})),
        slot,
        dict(raw.get("greens", {})),
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
                "kind": entry.kind,
                "job": entry.job,
                "covers_full_suite": entry.covers_full_suite,
            }
            for name, entry in state.entries.items()
        },
        "watermarks": dict(state.watermarks),
        "first_seen": dict(state.first_seen),
        "greens": dict(state.greens),
        "slot": {
            "gap_id": state.slot.gap_id,
            "task_id": state.slot.task_id,
            "unreadable_rounds": state.slot.unreadable_rounds,
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


# meiva:note
# The card's heading promises a range -- last green to latest red -- and one commit under it tells
# a reader the change that broke them is not in the list. So the round asks for the commits between
# the two whenever the last green is known, and keeps the red run's own line only as the fallback.
# A range that could not be read is reported as that, in the round's notes; it is never filled in
# from what the round happens to hold.
def _candidate_commits(
    run: RunFacts, compare: Compare | None, base: str | None
) -> tuple[tuple[str, ...], str | None]:
    head = run.head_sha
    if compare is None or not base or not head:
        return _commit_line(run), None
    span = f"{base[:10]}..{head[:10]}"
    try:
        commits = compare(base, head)
    except Exception as exc:
        return _commit_line(run), f"compare {span} unreadable: {type(exc).__name__}: {exc}"
    if not commits:
        return _commit_line(run), f"compare {span} named no commit"
    return commits, None


# meiva:note
# The card one confirmed red opens, together with the note its range collection owes the round.
# `base` is the newest green the watch has seen for the workflow; naming it on the entry is what
# turns the card's "Last green" line from a dash into the commit the range starts at.
def _confirmed_card(
    entry: Entry, run: RunFacts, compare: Compare | None, base: str | None
) -> tuple[Card, str | None]:
    candidates, note = _candidate_commits(run, compare, base)
    named = replace(entry, last_green_sha=base) if base else entry
    return (
        Card(
            named.fingerprint,
            card_title(named),
            card_body(
                named,
                latest_url=run.html_url,
                job_names=run.job_names,
                candidate_commits=candidates,
            ),
            # meiva:note
            # This red's own start, not the sha: a test that goes red again after a green is a new
            # occurrence of the same fingerprint, and the old card's key must not claim it.
            episode=named.first_seen_at.isoformat(),
        ),
        note,
    )


def round_line(
    *, entries: int, confirmed: int, infra: int, notes: tuple[str, ...], unnamed: int = 0
) -> str:
    # meiva:note
    # `unnamed` is the count that watches the watch: a class-mapping that keeps missing steps shows
    # up here as a number that grows, because a repeat of a shape already on a card only increments
    # its count and produces no second card. A flat zero says the mappings still cover what fails.
    head = f"fingerprints={entries} confirmed={confirmed} infra={infra} unnamed={unnamed}"
    return head if not notes else f"{head} | " + " | ".join(notes)


# meiva:note
# One run's newest attempt, read as `(attempt number, that attempt's jobs)`. The read lives with the
# other `gh` calls; the judgement on what it returns lives here, where a test can hand it values.
AttemptOf = Callable[[int], tuple[int, tuple[Mapping[str, object], ...]]]


# meiva:note
# The gate a re-run earned (task #6488, 405 2026-11-08): a card is opened only while the job that
# reported the red is still red in the newest attempt of its own run. This is the case that made it
# necessary -- `e2e full matrix (1/4)` failed in attempt 1, attempt 2 cleared it thirty minutes
# later, and the card had been opened in between; the aggregate `e2e gate` job was green in that
# same attempt 2 while shard 3 stayed red, which is why the question is asked per job and never of
# the run's own conclusion.
#
# It answers `ATTEMPT_HOLDS` whenever it cannot know better -- no port, no job on the entry, no run
# id, or an attempt that could not be read -- so the one thing able to suppress a card is a green
# somebody actually read. The round line carries the decision either way, so a gate that never runs
# (an entry from a ledger that predates the field) is visible rather than silently absent.
def _attempt_decision(
    entry: Entry, run: RunFacts, attempt_of: AttemptOf | None, notes: list[str]
) -> str:
    if attempt_of is None or not entry.job or not run.run_id:
        return ATTEMPT_HOLDS
    try:
        attempt, jobs = attempt_of(run.run_id)
    except Exception as exc:
        notes.append(f"attempt of run {run.run_id} unreadable: {type(exc).__name__}: {exc}")
        return ATTEMPT_HOLDS
    verdict, seen = job_attempt_verdict(jobs, entry.job)
    if verdict == ATTEMPT_HOLDS:
        if attempt > 1:
            notes.append(f"{entry.fingerprint}: {entry.job} attempt {attempt} {seen}")
        return ATTEMPT_HOLDS
    if verdict == ATTEMPT_GREEN:
        notes.append(
            f"{entry.fingerprint}: {entry.job} attempt {attempt} green, claim withdrawn, no card"
        )
    else:
        notes.append(f"{entry.fingerprint}: {entry.job} attempt {attempt} {seen}, card waits")
    return verdict


# meiva:note
# One run's effects, kept out of the round so the round stays a loop over streams: what a run does
# to the ledger is apply the verdict, keep the newest green, and act on the cards it confirms or
# closes. `unnamed` is the count of reds the round could not put a name on -- a repeat of a shape
# already on a card only increments it, so this number is the only place a stale mapping shows.
def _apply_one_run(
    entries: dict[str, Entry],
    run: RunFacts,
    *,
    greens: dict[str, str],
    compare: Compare | None,
    open_card: Callable[[Card], int],
    close_card: Callable[[int, str], None],
    notes: list[str],
    attempt_of: AttemptOf | None = None,
) -> tuple[dict[str, Entry], int, int, int]:
    plan = apply_run(entries, run)
    entries = dict(plan.entries)
    if run.conclusion == "success" and run.covers_full_suite and run.head_sha:
        greens[run.workflow] = run.head_sha
    infra = sum(1 for note in plan.notes if "infra on" in note)
    unnamed = sum(1 for failure in run.failures if failure.kind == FAILURE_UNNAMED)
    opened = 0
    for entry in plan.confirmed:
        decision = _attempt_decision(entry, run, attempt_of, notes)
        if decision == ATTEMPT_PENDING:
            continue
        if decision == ATTEMPT_GREEN:
            # meiva:note
            # The claim is withdrawn, so the entry goes with it: nothing is left to card, and a
            # later red of the same case is a new sighting rather than a second strike earned by a
            # run whose own re-run said it passed.
            entries.pop(entry.fingerprint, None)
            continue
        card, candidate_note = _confirmed_card(entry, run, compare, greens.get(run.workflow))
        if candidate_note is not None:
            notes.append(candidate_note)
        task_id = open_card(card)
        entries[entry.fingerprint] = replace(entries[entry.fingerprint], task_id=task_id)
        opened += 1
        notes.append(f"opened card {task_id} for {entry.fingerprint}")
    for task_id in plan.closes:
        close_card(task_id, "resolved by a green run")
        notes.append(f"closed card {task_id}")
    return entries, opened, infra, unnamed


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
    compare: Compare | None = None,
    attempt_of: AttemptOf | None = None,
) -> str:
    state = load_state(state_path)
    entries = dict(state.entries)
    watermarks = dict(state.watermarks)
    first_seen = dict(state.first_seen)
    greens = dict(state.greens)
    for workflow in (*workflows, *(stream.workflow for stream in liveness)):
        first_seen.setdefault(workflow, now.isoformat())
    notes: list[str] = []
    stale = 0
    opened = 0
    infra = 0
    unnamed = 0
    for workflow in workflows:
        seen_at, seen_attempt = _read_watermark(watermarks.get(workflow))
        try:
            runs = fetch(workflow)
        except Exception as exc:
            notes.append(f"stream {workflow} unreadable: {type(exc).__name__}: {exc}")
            stale += 1
            continue
        newest: tuple[datetime, int | None] | None = (
            (seen_at, seen_attempt) if seen_at is not None else None
        )
        for run in reversed(runs):
            if _already_read(run, seen_at, seen_attempt):
                continue
            if newest is None or (run.created_at, run.attempt) > (newest[0], newest[1] or 0):
                newest = (run.created_at, run.attempt)
            entries, run_opened, run_infra, run_unnamed = _apply_one_run(
                entries,
                run,
                greens=greens,
                compare=compare,
                open_card=open_card,
                close_card=close_card,
                notes=notes,
                attempt_of=attempt_of,
            )
            opened += run_opened
            infra += run_infra
            unnamed += run_unnamed
        if newest is not None:
            watermarks[workflow] = _format_watermark(newest[0], newest[1])
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
            make_up=stream.make_up,
        )
        if alert is not None:
            notes.append(alert)
            stale += 1
    save_state(state_path, WatchState(entries, watermarks, first_seen, greens=greens))
    emit(
        {
            "fingerprints": len(entries),
            "confirmed": opened,
            "infra": infra,
            "stale": stale,
            "unnamed": unnamed,
        }
    )
    return round_line(
        entries=len(entries), confirmed=opened, infra=infra, notes=tuple(notes), unnamed=unnamed
    )


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
            close_card(ledger.task_id, decision.note or "a scheduled release-gate run appeared")
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
