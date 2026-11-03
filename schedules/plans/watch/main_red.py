# meiva:note
# What the main-red watch decides: which failures a post-merge run reports, whether a red is a
# real test failure or infrastructure noise, and when a red becomes a card somebody has to take.
#
# The post-merge runs are the only evidence that `main` is green: `ci.yml` triggers on
# `pull_request` alone, so a merge starts no test of its own. On 2026-11-01 two scheduled
# `release-gate` runs went red on `main` and nobody saw either; the later one was a single test
# out of 1344 that the nightly passed on the same commit an hour later.
#
# Two rules follow from that shape. A red is believed only when no green run of the same commit
# explains it, and a failure is identified by (workflow, test id) alone -- never by the shard it
# ran in, because shard membership moves as tests are added, and never by the commit, because an
# unfixed red reappears on every new commit and would look new each time.
# meiva:link schedules/README.md

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

__all__ = [
    "FAILURE_INFRA",
    "FAILURE_TEST",
    "NIGHTLY_STALE_AFTER",
    "RELEASE_GATE_STALE_AFTER",
    "STATE_CONFIRMED",
    "STATE_INFRA",
    "STATE_SUSPECTED",
    "WATCH_STALE_AFTER",
    "Card",
    "Entry",
    "Failure",
    "Plan",
    "RunFacts",
    "apply_run",
    "card_body",
    "card_title",
    "fingerprint",
    "liveness_alert",
    "parse_failures",
]

FAILURE_TEST = "test"
FAILURE_INFRA = "infra"

# meiva:note
# The watch's own heartbeat, in missed slots: a round lands every half hour, so one hour is two
# missed slots. One missed slot is a slow round; two mean the watch stopped reporting.
WATCH_STALE_AFTER = timedelta(hours=1)


STATE_SUSPECTED = "suspected"
STATE_CONFIRMED = "confirmed"
STATE_INFRA = "infra"

_SEPARATOR = "::"

# meiva:note
# The two shapes a failing case is printed in: pytest's own `FAILED path::node`, and the native
# JUnit validator's `FAILED package.module::node`. Both were observed on 2026-11-01.
_FAILED_LINE = re.compile(r"^\s*FAILED\s+(?P<test_id>[^\s(]+)", re.MULTILINE)

# meiva:note
# Evidence that the run broke around the suite rather than in it: a container that never pulled
# its image, a disk that filled, a step that never started, an action the runner could not fetch
# (2026-11-03: a codeload failure killed a job in eight seconds with no test having run). Without this the first runner OOM
# wakes someone to hunt a regression that does not exist, which is how a watch loses its reader.
_INFRA_MARKERS = (
    "an action could not be found at the uri",
    "failed to download archive",
    "no space left on device",
    "unable to locate executable",
    "error response from daemon",
    "cannot connect to the docker daemon",
    "the runner has received a shutdown signal",
    "lost communication with the server",
)

# meiva:note
# How long `main` may go without a new post-merge start before a round reports the schedule
# instead of the code. Nine hours is one six-hour slot plus three hours of known GitHub schedule
# delay: on 2026-11-01 the scheduled runs appeared at 17:56Z and 22:05Z, and by 12:12Z the next
# day the 00:17Z slot had still not started.
RELEASE_GATE_STALE_AFTER = timedelta(hours=9)
NIGHTLY_STALE_AFTER = timedelta(hours=27)

_CANDIDATE_LIMIT = 20


# meiva:note
# One failing case out of one run. `kind` is the only judgement made from the log: `test` means a
# JUnit record names it, `infra` means the run broke around the suite instead.
@dataclass(frozen=True)
class Failure:
    test_id: str
    kind: str


# meiva:note
# One post-merge run, reduced to what a decision needs. An empty `failures` on a red run means the
# log named nothing, which is not the same as "no failure" -- see `parse_failures`.
@dataclass(frozen=True)
class RunFacts:
    workflow: str
    head_sha: str
    created_at: datetime
    conclusion: str
    failures: tuple[Failure, ...]
    html_url: str | None = None
    job_names: tuple[str, ...] = ()
    covers_full_suite: bool = True
    head_title: str = ""


# meiva:note
# One fingerprint's history on `main`. `task_id` is written when the red is confirmed and kept
# after it resolves, so a later red of the same case can be traced back to the card it opened.
@dataclass(frozen=True)
class Entry:
    fingerprint: str
    state: str
    first_seen_sha: str
    first_seen_at: datetime
    last_red_sha: str
    last_red_at: datetime
    red_count: int
    last_green_sha: str | None = None
    last_green_at: datetime | None = None
    task_id: int | None = None


@dataclass(frozen=True)
class Card:
    fingerprint: str
    title: str
    body: str

    # meiva:note
    # The card for a watch whose rounds stopped. It lives here, on the type it builds, rather than
    # as another module-level decision: `main_red` sits at the module surface budget, and this
    # question shares the card vocabulary with every other card the watch opens. A watch with no
    # round on record returns nothing -- a schedule that never ran is the manager's business, and a
    # fresh site would otherwise open a card the first time the check ran.
    @classmethod
    def for_stale_watch(
        cls, last_round_at: datetime | None, now: datetime, *, stale_after: timedelta
    ) -> Card | None:
        if last_round_at is None:
            return None
        age = now - last_round_at
        if age < stale_after:
            return None
        hours = stale_after.total_seconds() / 3600
        return cls(
            fingerprint="main-red-watch/liveness",
            title="The main-red watch stopped reporting: no round heartbeat",
            body=(
                f"The watch's last round was {age.total_seconds() / 3600:.1f}h ago "
                f"(threshold {hours:.0f}h, newest {last_round_at.isoformat()}).\n\n"
                "This reports the watch, not `main`: while it is down, a red on `main` opens no "
                "card.\n\n"
                "Check, in this order:\n\n"
                "1. `meiva schedules runs <id>` -- is the entry running, and did its last session "
                "end `interrupted`?\n"
                "2. `meiva schedules logs <id>` -- does a round print its `fingerprints=...` line, "
                "or does it raise before printing?\n"
                "3. `gh api repos/zhiyuan-zhang0206/Meiva/actions/workflows/release-gate.yml/runs` "
                "-- if GitHub itself is down or the workflow was disabled, the watch has nothing "
                "to read and says so in its own round.\n\n"
                "Closes when a round lands again."
            ),
        )


# meiva:note
# What one round changes: the new entry table, the fingerprints that just became confirmed, the
# task ids whose red ended, and the lines that explain why nothing was reported.
@dataclass(frozen=True)
class Plan:
    entries: tuple[tuple[str, Entry], ...]
    confirmed: tuple[Entry, ...]
    closes: tuple[int, ...]
    notes: tuple[str, ...]


def fingerprint(workflow: str, test_id: str) -> str:
    return f"{workflow}{_SEPARATOR}{test_id}"


def _infrastructure_marker(log_text: str) -> str | None:
    lowered = log_text.lower()
    for marker in _INFRA_MARKERS:
        if marker in lowered:
            return marker
    return None


# meiva:note
# The steps of the push gate that are themselves the checks. A gate run cannot report a JUnit
# failure for `meizel check` — it prints no `FAILED <test>` line — so the step that failed has to
# name the red, or a real violation would be read as an infra fault and open no card.
GATE_CHECK_STEPS = frozenset(
    {
        "Locality gate (full meizel check)",
        "Every hook script enters a scratch home before application code",
    }
)


def parse_failures(
    log_text: str,
    *,
    job_name: str,
    suite_step_failed: bool,
    failed_steps: tuple[str, ...] = (),
    check_steps: frozenset[str] = frozenset(),
) -> tuple[Failure, ...]:
    marker = _infrastructure_marker(log_text)
    if marker is not None:
        return (Failure(f"{job_name} runner: {marker}", FAILURE_INFRA),)
    found = tuple(
        Failure(match.group("test_id").strip(), FAILURE_TEST)
        for match in _FAILED_LINE.finditer(log_text)
    )
    if found:
        return found
    # meiva:note
    # A named check step failing is a red of that check, not of the runner. Only a workflow that
    # names its check steps gets here: for the full-suite workflows an unnamed failure stays infra,
    # because their steps include installs that fail for reasons that are nobody's regression.
    for step in failed_steps:
        if step in check_steps:
            return (Failure(f"{job_name} / {step}", FAILURE_TEST),)
    if suite_step_failed:
        return (Failure(f"{job_name}: unnamed failure", FAILURE_INFRA),)
    return ()


def _unique(failures: tuple[Failure, ...]) -> tuple[Failure, ...]:
    seen: dict[str, Failure] = {}
    for failure in failures:
        seen.setdefault(failure.test_id, failure)
    return tuple(seen.values())


# meiva:note
# A green run ends every open red that started at or before it, whichever post-merge workflow
# found the red: both run the whole population, so a green nightly on a commit where a release-gate
# run failed is exactly the evidence that the commit was fine (2026-11-01, b32d5a8662). Only a
# workflow that covers the full suite may resolve, and an older run never resolves a newer red.
def _resolve_green(plan_entries: dict[str, Entry], run: RunFacts) -> tuple[int, ...]:
    if not run.covers_full_suite:
        return ()
    closes: list[int] = []
    for name, entry in list(plan_entries.items()):
        if entry.state == STATE_INFRA:
            continue
        if run.created_at < entry.last_red_at:
            continue
        if entry.task_id is not None:
            closes.append(entry.task_id)
        del plan_entries[name]
    return tuple(closes)


def apply_run(entries: dict[str, Entry], run: RunFacts) -> Plan:
    plan_entries = dict(entries)
    notes: list[str] = []
    confirmed: list[Entry] = []
    if run.conclusion == "success":
        closes = _resolve_green(plan_entries, run)
        for task_id in closes:
            notes.append(f"green run {run.head_sha} closes card {task_id}")
        return Plan(tuple(plan_entries.items()), (), closes, tuple(notes))
    for failure in _unique(run.failures):
        if failure.kind == FAILURE_INFRA:
            notes.append(f"infra on {run.head_sha}: {failure.test_id}")
            continue
        name = fingerprint(run.workflow, failure.test_id)
        entry = plan_entries.get(name)
        if entry is None:
            plan_entries[name] = Entry(
                fingerprint=name,
                state=STATE_SUSPECTED,
                first_seen_sha=run.head_sha,
                first_seen_at=run.created_at,
                last_red_sha=run.head_sha,
                last_red_at=run.created_at,
                red_count=1,
            )
            notes.append(f"suspected {name} on {run.head_sha}")
            continue
        updated = replace(
            entry,
            last_red_sha=run.head_sha,
            last_red_at=run.created_at,
            red_count=entry.red_count + 1,
        )
        if entry.state == STATE_SUSPECTED and entry.first_seen_sha != run.head_sha:
            updated = replace(updated, state=STATE_CONFIRMED)
            confirmed.append(updated)
            notes.append(f"confirmed {name} after {updated.red_count} red runs")
        plan_entries[name] = updated
    if run.conclusion == "failure" and not _unique(run.failures):
        notes.append(f"red run {run.head_sha} named no failure: nothing recorded")
    return Plan(tuple(plan_entries.items()), tuple(confirmed), (), tuple(notes))


def card_title(entry: Entry) -> str:
    workflow, _, test_id = entry.fingerprint.partition(_SEPARATOR)
    return f"main-red: {test_id} failed {entry.red_count} post-merge runs on main ({workflow})"


def card_body(
    entry: Entry,
    *,
    latest_url: str | None,
    job_names: tuple[str, ...],
    candidate_commits: tuple[str, ...],
) -> str:
    workflow, _, test_id = entry.fingerprint.partition(_SEPARATOR)
    jobs = ", ".join(job_names) or "-"
    commits = "\n".join(candidate_commits[:_CANDIDATE_LIMIT]) or "(not collected)"
    return (
        f"Fingerprint: `{entry.fingerprint}`\n\n"
        f"Evidence: {latest_url or '-'} (jobs: {jobs})\n\n"
        f"First seen: `{entry.first_seen_sha}` at {entry.first_seen_at.isoformat()}\n\n"
        f"Latest red: `{entry.last_red_sha}` at {entry.last_red_at.isoformat()}\n\n"
        f"Red runs: {entry.red_count}\n\n"
        f"Last green: `{entry.last_green_sha or '-'}`\n\n"
        f"Candidate commits ({workflow}, last green to latest red, {_CANDIDATE_LIMIT} lines max):\n\n"
        f"```\n{commits}\n```\n\n"
        f"Class: `{FAILURE_TEST}`\n\n"
        f"Closes when: `{test_id}` passes on `main` in any post-merge workflow; the card closes itself."
    )


# meiva:note
# `since` is when this watch first observed the stream. A stream that has no run on record is only
# a stopped schedule once one threshold has passed since then: before that the cron has simply not
# come round yet, and a card about it would name a problem nobody can act on. Streams older than
# the watch itself pass no `since`, and their silence means what it always did.
def liveness_alert(
    label: str,
    last_start: datetime | None,
    now: datetime,
    *,
    stale_after: timedelta,
    since: datetime | None = None,
) -> str | None:
    if last_start is None:
        if since is not None and now - since < stale_after:
            return None
        watching = f" since {since.isoformat()}" if since is not None else " at all"
        return f"{label} has no run on record{watching}: the schedule never started here."
    age = now - last_start
    if age < stale_after:
        return None
    return (
        f"{label} has had no new run for {age.total_seconds() / 3600:.1f}h "
        f"(threshold {stale_after.total_seconds() / 3600:.0f}h, newest {last_start.isoformat()}). "
        "This reports the schedule, not the code: check whether GitHub delayed or disabled it."
    )
