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
    "FAILURE_UNNAMED",
    "NIGHTLY_STALE_AFTER",
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
# A red run whose log named nothing the watch could read is neither a flake nor the runner: the run
# failed and no parse put a name on it. Two strikes would buy a night of silence for exactly the
# failure that once hid a four-hour structure red on `main`, so this class reports on its first
# sighting -- the fingerprint keeps it to one card -- and its id names the steps a mapping would
# have to cover, which is how the mapping stops rotting unnoticed.
FAILURE_UNNAMED = "unnamed"

# meiva:note
# The watch's own heartbeat, in missed slots: a round lands every half hour, so one hour is two
# missed slots. One missed slot is a slow round; two mean the watch stopped reporting.
WATCH_STALE_AFTER = timedelta(hours=1)


STATE_SUSPECTED = "suspected"
STATE_CONFIRMED = "confirmed"
STATE_INFRA = "infra"

_SEPARATOR = "::"

# meiva:note
# The shapes a failing case is printed in: pytest's own `FAILED path::node`, the native JUnit
# validator's `FAILED package.module::node`, and those same lines as the job log delivers them --
# each line carries the run's `<ISO-8601>Z ` timestamp, and xdist's progress lines add
# `[gw1] [ 24%] ` in front. Reading only the bare form made every delivered failure unreadable:
# on 2026-11-06 a 214 KB job log held 37 `FAILED` lines and the watch matched none of them, so
# every red job was filed as one unnamed infrastructure fault and no card could ever open.
# meiva:note
# pytest colours `FAILED` when its output is not a pipe, and the job log keeps the escapes: the line
# reads `\x1b[31mFAILED path::node\x1b[0m`, so every one of the anchored patterns below misses it.
# On 2026-11-08 three cards opened as `unnamed failure` from logs that each carried the case -- the
# backend logs printed `FAILED gateway/tests/...::test_...` and the watch read none of them. The
# escapes are stripped at the boundary, once, rather than taught to every pattern.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

_FAILED_LINE = re.compile(
    r"^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z\s+)?"
    r"(?:\[gw\d+\]\s+\[\s*\d+%\]\s+)?"
    r"\s*FAILED\s+(?P<test_id>[^\s(]+)",
    re.MULTILINE,
)

# meiva:note
# pytest's own summary banner. A job that printed it ran the suite, so a summary that names no
# case is the watch failing to read a failure -- not a runner fault -- and the round must say which.
_PYTEST_SUMMARY = "short test summary info"

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
# How long a stream may go without a new run before a round reports the schedule instead of the
# code. The release gate is not here: a six-hourly cron that stopped arriving is a missing slot
# with a make-up run to dispatch, which `release_gate_slot` owns, and one threshold for that one
# workflow should not live in two modules.
NIGHTLY_STALE_AFTER = timedelta(hours=27)

_CANDIDATE_LIMIT = 20


# meiva:note
# What the run's JUnit artifact offered for one job, as one value: the cases it named, and -- when
# no artifact could be tied to the job -- the near misses a reader should open instead. One
# parameter rather than two because they travel together and `parse_failures` is at its parameter
# budget; a caller that passes candidates without cases is a mistake this type cannot express.
@dataclass(frozen=True)
class JunitEvidence:
    case_ids: tuple[str, ...] = ()
    candidates: tuple[str, ...] = ()


_NO_JUNIT = JunitEvidence()


# meiva:note
# One failing case out of one run. `kind` is the only judgement made from the log: `test` means a
# JUnit record names it, `infra` means the run broke around the suite instead.
@dataclass(frozen=True)
class Failure:
    test_id: str
    kind: str
    # meiva:note
    # Which job in the run reported this. The card's fingerprint stays (workflow, test_id) -- a
    # shard name must not decide identity -- but a decision about the job itself (is it downstream
    # of another red?) needs the job as a field, not as a prefix of `test_id`.
    job: str = ""
    # meiva:note
    # Whether the job that reported this failure ran the population. A workflow's nightly verdict
    # can carry a job that runs part of the tree (the macOS data-plane job inside `ci.yml`), and a
    # red from it belongs to that job: the next green of the workflow proves nothing about a test
    # the job alone collects.
    covers_full_suite: bool = True
    # meiva:note
    # The artifacts a reader should look at when this red could not be named: names from the same
    # run that share a word with the job. Capped at the plan's own limit, because a run can publish
    # dozens and the card is read by a person. Empty for a failure the log or the JUnit named.
    artifact_candidates: tuple[str, ...] = ()


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
    # meiva:note
    # Failing jobs that a job they `needs` also failed in this run: they started only because the
    # workflow's dependency graph let them, so their red is the upstream red seen once more. See
    # `downstream_failing_jobs` for why the graph, and not timing, is what says so.
    downstream_jobs: tuple[str, ...] = ()
    covers_full_suite: bool = True
    head_title: str = ""
    # meiva:note
    # Which attempt of this run produced the facts above, and the run it belongs to. A re-run keeps
    # the run's id and its `created_at` and only advances `run_attempt`, so a watermark on time alone
    # can never see the second attempt — the red it left behind stands until some later run resolves
    # it (task #6488). `attempt=1` is what every record written before this field means.
    run_id: int = 0
    attempt: int = 1


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
    kind: str = FAILURE_TEST
    # meiva:note
    # The job that reported this red, from the run that first named it. A re-run advances one job and
    # leaves the run alone, so "did this claim survive the re-run" is a question about this job in
    # that run's newest attempt -- and answering it needs the name, which the fingerprint does not
    # carry (that stays `(workflow, test_id)`, because a shard name must not decide identity). An
    # entry written before the field existed has none: the gate then leaves it alone rather than
    # re-deciding a red on evidence nobody had when the red was recorded.
    job: str = ""
    # meiva:note
    # The run that last reported this red. A card that is already open has to be asked about
    # afterwards too: a re-run can turn its job green while no later run exists to retire the card,
    # and without the id the watch would have to guess which run to ask. Zero means an entry from a
    # ledger that predates the field, and nothing asks about it.
    run_id: int = 0
    # meiva:note
    # How many completed runs of this workflow have been read since the red was first seen -- the
    # chances this watch had to say something new about it. `suspected` has no exit of its own: it
    # waits for a second red, and if `main` stops moving, or the schedule stops, that second red
    # never comes and nothing reports the wait. A zero here is the executable form of "no chance to
    # re-check", which is a fact the round can state (task #6488, 405 2026-11-08).
    #
    # It counts runs this watch *read*, added to each round, because after #6509 the fetch returns
    # only runs the watermark has not covered: recomputing from a listing would count the same run
    # again every round, while adding what the round read counts each run once.
    opportunities: int = 0
    # meiva:note
    # Whether the run that reported this red ran the whole population. It decides what may retire
    # the card: a full-suite red is evidence about the whole tree, so any full-suite green on a
    # later commit resolves it, while a red from a lane that runs part of the tree is resolved only
    # by that lane's own green. Without this the macOS lane's red would be retired by the next
    # Linux nightly, which never collected the test — a card that closes while the red stands.
    covers_full_suite: bool = True
    # meiva:note
    # The artifacts a reader should open when this red was never named (`kind=unnamed`): names from
    # the same run that share a word with the job. Carried on the entry rather than recomputed at
    # card time because the run's artifact list is gone once GitHub expires it -- the card would
    # then say "I do not know" with no way to recover what was on offer when the red was seen.
    artifact_candidates: tuple[str, ...] = ()

    # meiva:note
    # The workflow this entry belongs to, read back out of the fingerprint that encodes it. A
    # property rather than another module-level function: `main_red` is at its public surface
    # budget, and the separator has exactly one owner (`fingerprint`).
    @property
    def workflow(self) -> str:
        return self.fingerprint.partition(_SEPARATOR)[0]


# meiva:note
# The one field of the liveness card that never changes: its body carries the age of the last round,
# so a second `create` with today's body is a different call. Whoever closes the card finds it by
# this title.
STALE_WATCH_TITLE = "The main-red watch stopped reporting: no round heartbeat"


@dataclass(frozen=True)
class Card:
    fingerprint: str
    title: str
    body: str
    # meiva:note
    # Which occurrence of what `fingerprint` names this card reports: the red run, the stale
    # watch's last round, the missing slot. The fingerprint says what to watch and stays the same
    # across occurrences, so it cannot be the create key on its own -- the second occurrence would
    # reuse the first card's key with different text, and the registry refuses that (2026-11-05/06,
    # 38 rounds). `episode` is the part that moves.
    episode: str

    # meiva:note
    # The card for a watch whose rounds stopped. It lives here, on the type it builds, rather than
    # as another module-level decision: `main_red` sits at the module surface budget, and this
    # question shares the card vocabulary with every other card the watch opens. A watch with no
    # round on record returns nothing -- a schedule that never ran is the manager's business, and a
    # fresh site would otherwise open a card the first time the check ran.
    #
    # `now` decides whether a card is due, never what it says: the age of the last round grows every
    # round, and text that grows would make each round a different create under one key.
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
            title=STALE_WATCH_TITLE,
            body=(
                f"The watch's last round was at {last_round_at.isoformat()} "
                f"(threshold {hours:.0f}h).\n\n"
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
            episode=last_round_at.isoformat(),
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
    junit: JunitEvidence = _NO_JUNIT,
) -> tuple[Failure, ...]:
    log_text = _ANSI_ESCAPE.sub("", log_text)
    marker = _infrastructure_marker(log_text)
    if marker is not None:
        return (Failure(f"{job_name} runner: {marker}", FAILURE_INFRA, job_name),)
    found = tuple(
        Failure(match.group("test_id").strip(), FAILURE_TEST, job_name)
        for match in _FAILED_LINE.finditer(log_text)
    )
    if found:
        return found
    # meiva:note
    # A named check step failing is a red of that check, not of the runner.
    for step in failed_steps:
        if step in check_steps:
            return (Failure(f"{job_name} / {step}", FAILURE_TEST, job_name),)
    # meiva:note
    # The run's own JUnit artifact, when the log named nothing (task #6487). The id is built the way
    # the log builds it -- `file::name` -- so one test keeps one fingerprint whichever route named
    # it: a dotted `classname` would make the same case look like two, and the second sighting that
    # turns a suspect into a card would never arrive. An artifact that carried no failing case is
    # not the same as one that was never read, so it falls through to the unnamed failure below and
    # says which artifact was consulted.
    if junit.case_ids:
        return tuple(Failure(case, FAILURE_TEST, job_name) for case in junit.case_ids)
    # meiva:note
    # Everything below is a red this watch cannot name, and the steps are the whole of what it
    # knows: an empty tuple here is how a failed run once left no trace at all, so the answer is a
    # failure that carries the step names -- the fingerprint of a red that names nothing is the
    # pair (job, steps), which keeps one card per unreadable shape.
    steps = _steps_label(failed_steps)
    if suite_step_failed and _PYTEST_SUMMARY in log_text:
        return (
            Failure(
                f"{job_name}: pytest summary without a parsable case ({steps})",
                FAILURE_UNNAMED,
                job_name,
                artifact_candidates=junit.candidates,
            ),
        )
    if suite_step_failed or failed_steps:
        return (
            Failure(
                f"{job_name}: unnamed failure ({steps})",
                FAILURE_UNNAMED,
                job_name,
                artifact_candidates=junit.candidates,
            ),
        )
    return ()


def _steps_label(failed_steps: tuple[str, ...]) -> str:
    return ", ".join(failed_steps) if failed_steps else "no failed step reported"


def _unique(failures: tuple[Failure, ...]) -> tuple[Failure, ...]:
    seen: dict[str, Failure] = {}
    for failure in failures:
        seen.setdefault(failure.test_id, failure)
    return tuple(seen.values())


# meiva:note
# A green run ends the reds it is evidence about, and an older run never resolves a newer red. Its
# own workflow always counts: the lane re-ran and passed. Another workflow counts only when both
# sides run the whole population — a green nightly on a commit where a release-gate run failed is
# exactly the evidence that the commit was fine (2026-11-01, b32d5a8662) — because a run that never
# collected the test says nothing about it. The two facts are on the records themselves
# (`covers_full_suite` on the run and on the entry), so a reader can check the card's sentence
# against the same numbers.
def _green_ran_the_red(run: RunFacts, entry: Entry) -> bool:
    if run.workflow == entry.fingerprint.partition(_SEPARATOR)[0]:
        return True
    return run.covers_full_suite and entry.covers_full_suite


def _resolve_green(plan_entries: dict[str, Entry], run: RunFacts) -> tuple[int, ...]:
    closes: list[int] = []
    for name, entry in list(plan_entries.items()):
        if entry.state == STATE_INFRA:
            continue
        if run.created_at < entry.last_red_at:
            continue
        if not _green_ran_the_red(run, entry):
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
        # meiva:note
        # One event, one card. An aggregate job that failed only because a job it needs failed is
        # the same event, and its name still reaches the surviving card through `job_names`, so
        # nothing is lost by not carding it. Only an unnamed red is folded: a failure the watch
        # could name a case for is evidence in its own right, whatever ran before it.
        if failure.kind == FAILURE_UNNAMED and failure.job in run.downstream_jobs:
            notes.append(
                f"{failure.job} failed downstream of another red job in {run.head_sha}: folded"
            )
            continue
        name = fingerprint(run.workflow, failure.test_id)
        entry = plan_entries.get(name)
        if entry is None:
            # meiva:note
            # An unnameable red is confirmed on its first sighting: there is nothing to wait for a
            # second time, because the doubt two strikes answer -- "is this case flaky?" -- does not
            # exist when the doubt is "can the watch read this log at all". Every other class keeps
            # the second sighting, which is what stops one bad runner from opening a card.
            state = STATE_CONFIRMED if failure.kind == FAILURE_UNNAMED else STATE_SUSPECTED
            created = Entry(
                fingerprint=name,
                state=state,
                first_seen_sha=run.head_sha,
                first_seen_at=run.created_at,
                last_red_sha=run.head_sha,
                last_red_at=run.created_at,
                red_count=1,
                kind=failure.kind,
                job=failure.job,
                run_id=run.run_id,
                covers_full_suite=run.covers_full_suite and failure.covers_full_suite,
                artifact_candidates=failure.artifact_candidates,
            )
            plan_entries[name] = created
            if state == STATE_CONFIRMED:
                confirmed.append(created)
                notes.append(f"confirmed {name} on its first red run ({failure.kind})")
            else:
                notes.append(f"suspected {name} on {run.head_sha}")
            continue
        # meiva:note
        # The job and the run move with the red: a later sighting may come from another job of the
        # same fingerprint, and the question asked of a re-run is what the newest evidence named.
        updated = replace(
            entry,
            last_red_sha=run.head_sha,
            last_red_at=run.created_at,
            red_count=entry.red_count + 1,
            job=failure.job,
            run_id=run.run_id,
        )
        # meiva:note
        # Two red runs confirm, whatever commits they ran on. The round only reads runs newer than
        # its watermark, so a second sighting is a second run -- the sha test added nothing there,
        # and it kept a red invisible exactly when `main` stopped moving: an unchanged tree could
        # red every night and stay `suspected` forever.
        if entry.state == STATE_SUSPECTED:
            updated = replace(updated, state=STATE_CONFIRMED)
            confirmed.append(updated)
            notes.append(f"confirmed {name} after {updated.red_count} red runs")
        plan_entries[name] = updated
    if run.conclusion == "failure" and not _unique(run.failures):
        # meiva:note
        # Only reachable when a red run had no failing job to parse at all: every failing job now
        # yields at least an `unnamed` failure, so this line is the backstop that says the round
        # still saw a red -- never the whole story it used to be.
        notes.append(f"red run {run.head_sha} had no failing job to parse: nothing recorded")
    return Plan(tuple(plan_entries.items()), tuple(confirmed), (), tuple(notes))


def card_title(entry: Entry) -> str:
    workflow, _, test_id = entry.fingerprint.partition(_SEPARATOR)
    return f"main-red: {test_id} failed {entry.red_count} post-merge runs on main ({workflow})"


# meiva:note
# What the card says about the artifact it could not use. Two answers, and the second is the one that
# matters: with candidates the reader is told where to look; with none the card states that this run
# published nothing that names the job, which is a fact about the workflow rather than about the
# watch. Both are only written for a red the watch could not name -- a named red needs neither.
_ARTIFACT_CANDIDATE_LIMIT = 6


def _artifact_line(entry: Entry) -> str:
    if entry.kind != FAILURE_UNNAMED:
        return ""
    if not entry.artifact_candidates:
        return "Artifacts: this run published none that belongs to this job.\n\n"
    named = ", ".join(f"`{name}`" for name in entry.artifact_candidates[:_ARTIFACT_CANDIDATE_LIMIT])
    return f"Candidate artifacts (none matched this job): {named}\n\n"


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
    # meiva:note
    # What closes an unnameable red cannot be "this case passes": no case names it. A green run that
    # exercises the red on a later commit is the only evidence that the tree it failed on was
    # repaired, and which runs exercise it is the entry's own record: the same workflow always does,
    # and a full-suite workflow does while `covers_full_suite=true`. The sentence names that
    # predicate and its fields, so a reader can check it against the card's numbers.
    closes = (
        "Closes when: `apply_run` reads a green run whose `created_at` is later than this entry's "
        "`last_red_at` and which exercises this red -- the same workflow, or another one while "
        "this entry keeps `covers_full_suite=true`; that run removes the entry and the card with "
        "it, and no human closes either. A lane that runs part of the tree is retired only by its "
        "own green, because a run that never collected the test says nothing about it. While it is "
        "open, the same unnameable shape only increments `red_count`."
        if entry.kind == FAILURE_UNNAMED
        else (
            f"Closes when: `{test_id}` passes on `main` in `{workflow}`"
            + (
                ", or in a full-suite workflow on the main branch"
                if entry.covers_full_suite
                else " (this red came from a lane that runs part of the tree, so only that lane's own green retires it)"
            )
            + "; the card closes itself."
        )
    )
    return (
        f"Fingerprint: `{entry.fingerprint}`\n\n"
        f"Evidence: {latest_url or '-'} (jobs: {jobs})\n\n"
        f"{_artifact_line(entry)}"
        f"First seen: `{entry.first_seen_sha}` at {entry.first_seen_at.isoformat()}\n\n"
        f"Latest red: `{entry.last_red_sha}` at {entry.last_red_at.isoformat()}\n\n"
        f"Red runs: {entry.red_count}\n\n"
        f"Last green: `{entry.last_green_sha or '-'}`\n\n"
        f"Candidate commits ({workflow}, last green to latest red, {_CANDIDATE_LIMIT} lines max):\n\n"
        f"```\n{commits}\n```\n\n"
        f"Class: `{entry.kind}`\n\n"
        f"{closes}"
    )


# meiva:note
# `since` is when this watch first observed the stream. A stream that has no run on record is only
# a stopped schedule once one threshold has passed since then: before that the cron has simply not
# come round yet, and a card about it would name a problem nobody can act on. Streams older than
# the watch itself pass no `since`, and their silence means what it always did.
#
# `make_up` is the command that closes the gap, supplied by the stream's owner because only it knows
# what its own workflow costs to run. A note that ends at "check whether GitHub delayed it" hands
# the reader a diagnosis and no action, which is the half of the report they already had.
def liveness_alert(
    label: str,
    last_start: datetime | None,
    now: datetime,
    *,
    stale_after: timedelta,
    since: datetime | None = None,
    make_up: str | None = None,
) -> str | None:
    action = f" Make-up: {make_up}" if make_up else ""
    if last_start is None:
        if since is not None and now - since < stale_after:
            return None
        watching = f" since {since.isoformat()}" if since is not None else " at all"
        return f"{label} has no run on record{watching}: the schedule never started here.{action}"
    age = now - last_start
    if age < stale_after:
        return None
    return (
        f"{label} has had no new run for {age.total_seconds() / 3600:.1f}h "
        f"(threshold {stale_after.total_seconds() / 3600:.0f}h, newest {last_start.isoformat()}). "
        "This reports the schedule, not the code: check whether GitHub delayed or disabled it."
        f"{action}"
    )
