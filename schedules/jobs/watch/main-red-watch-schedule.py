# meiva:note
# The main-red watch: every thirty minutes, read the newest post-merge runs of `main` and turn a
# red that no green run explains into one card for the P0 lead.
#
# It exists because the queue that used to verify a merged tree is gone (retired 2026-11-01) and
# `ci.yml` runs on no push to `main`: the six-hourly `release-gate`, the nightly duration refresh
# and the nightly full run of `ci.yml` are the only evidence that `main` is green, and on
# 2026-11-01 both scheduled runs of the gate went red without anyone seeing them. A red page is
# not a signal nobody is watching for; it is no signal at all.
#
# The decision lives in `schedules/plans/main_red.py` and the effects in
# `schedules/jobs/main_red_watch.py`; this entry only supplies the cluster's own values.
# meiva:link schedules/README.md

import os
import subprocess
import sys
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import meiva
from meiva_builtins.plugins.meiva_fleet import task_registry
from base.clock import SYSTEM_TIMERS
from base.daemon.schedules.inputs import ScheduleInputs
from base.host.boundary.json_values import is_object_dict, is_object_list
from base.config import settings
from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.jobs.watch.api_read import read_api
from schedules.jobs.watch.main_red_slot import slot_round
from schedules.jobs.watch.main_red_watch import (
    LivenessStream,
    already_read,
    STATE_FILE,
    card_operation_key,
    listing_attempt,
    watch_once,
)
from schedules.jobs.telemetry_event import emit_system_event
from schedules.plans.schedule_identity import parse_schedule_id
from schedules.jobs.watch.main_red_liveness import liveness_once
from schedules.plans.watch.main_red import (
    FAILURE_UNNAMED,
    JunitEvidence,
    parse_failures,
    STALE_WATCH_TITLE,
    Card,
    Failure,
    GATE_CHECK_STEPS,
    NIGHTLY_STALE_AFTER,
    RunFacts,
)
from schedules.plans.watch.junit import ArtifactMatch, failing_case_ids, match_artifact
from schedules.plans.watch.workflow_graph import downstream_failing_jobs
from schedules.plans.watch.release_gate_slot import RELEASE_GATE_SLOT_CADENCE, SlotFacts

RunProcess = Callable[..., subprocess.CompletedProcess[str]]

CRON = "*/30 * * * *"
# meiva:note
# The liveness slot sits five minutes after each round: far enough that a slow round has finished
# and printed, close enough that a stopped watch is reported inside the hour it went quiet.
LIVENESS_CRON = "5,35 * * * *"
_LIVENESS_PAYLOAD = "liveness"
_PROCESS_NAME = "main-red-watch"
_EVENT_NAME = "main_red_watch_round"
_RELEASE_GATE = "release-gate.yml"
_NIGHTLY = "refresh-test-durations.yml"
_CI_NIGHTLY = "ci.yml"
_MAIN_PUSH = "main-push.yml"
# meiva:note
# The jobs inside a workflow that run part of the tree rather than the population. A failure they
# report is not full-suite evidence even though the run it arrived in is: `ci.yml`'s nightly verdict
# includes the macOS data-plane job, and a red there is retired only by that job's own green -
# otherwise the next Linux nightly, which never collected those tests, would close the card while
# the red stood (2026-11-08, #8659/#8660). Keyed by job name because that is what a failed run
# reports: the run's jobs are the only record of which part of the tree a failure came from.
_PARTIAL_JOBS = frozenset({"data plane (macOS)"})
# meiva:note
# Only a workflow that runs the whole population may close a red; the push gate runs two checks,
# so its green run says nothing about the test a nightly run found. Reading it as verdict enough
# would close cards for failures nobody fixed. The macOS lane is the third shape: its green says
# its own tests pass, so it neither closes another stream's card nor opens one for a host it never
# touched - its reds are confirmed and closed against the full-suite runs like everything else.
_FULL_SUITE_WORKFLOWS = frozenset({_RELEASE_GATE, _NIGHTLY, _CI_NIGHTLY})
_REPORT_AGENT = 405
_ENV_REPORT_AGENT = "MEIVA_MAIN_RED_REPORT_AGENT"
_REPO = "zhiyuan-zhang0206/Meiva"
# meiva:note
# `ci.yml` also runs on every pull request, so its runs must be narrowed to the default branch:
# a red pull-request run is that pull request's business, and reading it here would open cards
# against `main` for a branch nobody merged.
_PR_RUNNING_WORKFLOWS = frozenset({_CI_NIGHTLY})


def _runs_path(workflow: str, page: int) -> str:
    # meiva:note
    # One place builds the listing path, so the newest run and the run history are narrowed alike:
    # a liveness read that saw a pull-request run would report the schedule alive while it is dead.
    branch = "&branch=main" if workflow in _PR_RUNNING_WORKFLOWS else ""
    return f"repos/{_REPO}/actions/workflows/{workflow}/runs?per_page={page}{branch}"


# meiva:note
# The lead the cards belong to. A site that reports elsewhere sets the variable; a value that is
# not a positive id stops the round naming the variable instead of opening cards into nothing.
def report_agent_from_environ(environ: Mapping[str, str]) -> int:
    raw = environ.get(_ENV_REPORT_AGENT, "").strip()
    if not raw:
        return _REPORT_AGENT
    try:
        agent_id = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{_ENV_REPORT_AGENT} must be a numeric agent id") from exc
    if agent_id <= 0:
        raise RuntimeError(f"{_ENV_REPORT_AGENT} must be a positive agent id")
    return agent_id


# meiva:note
# The watch reads GitHub through one function, so the retry policy has one owner: `read_api`
# delegates to the CI scripts' transport, which retries a transient answer within a fixed budget and
# then fails naming the endpoint and the number of attempts it took. A bare `subprocess.run` here
# would put a second answer to "how many times is a 502 worth retrying" in the repository.
def _api(path: str, run_process: RunProcess) -> str:
    return read_api(path, run_process=run_process, timers=SYSTEM_TIMERS)


# meiva:note
# The steps of a job that reported `failure`. The watch names the one that failed when it is a
# check of the push gate, so a red says which check, not only which job.
def _failed_step_names(job: Mapping[object, object]) -> tuple[str, ...]:
    steps = job.get("steps")
    if not is_object_list(steps):
        return ()
    names: list[str] = []
    for step in steps:
        if is_object_dict(step) and step.get("conclusion") == "failure":
            name = step.get("name")
            if isinstance(name, str):
                names.append(name)
    return tuple(names)


def _job_log(job_id: int, run_process: RunProcess) -> str:
    result = run_process(
        ["gh", "api", "--method", "GET", f"repos/{_REPO}/actions/jobs/{job_id}/logs"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result.stdout if result.returncode == 0 else ""


# meiva:note
# The workflow definition at the commit the run executed, so `needs` describes the graph that run
# actually used rather than the tree checked out here. An unreadable file yields "": the plan then
# folds no failure, which reports every job instead of dropping one -- the cheap direction.
def _workflow_text(workflow: str, sha: str, run_process: RunProcess) -> str:
    import base64
    import binascii
    import json

    if not sha:
        return ""
    try:
        payload = json.loads(
            _api(f"repos/{_REPO}/contents/.github/workflows/{workflow}?ref={sha}", run_process)
        )
    except (RuntimeError, ValueError):
        return ""
    content = payload.get("content") if is_object_dict(payload) else None
    if not isinstance(content, str):
        return ""
    try:
        return base64.b64decode(content).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


# meiva:note
# One job's red, from the log first and from the run's JUnit artifact only when the log named
# nothing (task #6487). The artifact leg is the second read because it costs a download and an
# unzip: a log that already said `FAILED path::case` has told the whole story. Both routes spell a
# case the same way, so the fingerprint a red gets does not depend on which one answered.
def _job_failures(
    job: Mapping[object, object],
    *,
    run_id: int,
    check_steps: frozenset[str],
    scratch: Path,
    run_process: RunProcess,
) -> tuple[Failure, ...]:
    job_id = job.get("id")
    if not isinstance(job_id, int):
        return ()
    raw_name = job.get("name")
    job_name = raw_name if isinstance(raw_name, str) else ""
    log = _job_log(job_id, run_process)
    failed_steps = _failed_step_names(job)
    from_log = parse_failures(
        log[-400_000:],
        job_name=job_name,
        suite_step_failed=True,
        failed_steps=failed_steps,
        check_steps=check_steps,
    )
    if from_log and all(failure.kind != FAILURE_UNNAMED for failure in from_log):
        return from_log
    case_ids, match = junit_case_ids(run_id, job_name, scratch, run_process)
    return parse_failures(
        log[-400_000:],
        job_name=job_name,
        suite_step_failed=True,
        failed_steps=failed_steps,
        check_steps=check_steps,
        junit=JunitEvidence(case_ids, match.candidates),
    )


# meiva:note
# The artifacts one run published, as GitHub lists them. A run that published none answers an empty
# tuple, which reads as "no artifact named this job" -- the same evidence as an artifact that is
# missing, and the caller already knows how to say that.
def _run_artifacts(run_id: int, run_process: RunProcess) -> tuple[dict[str, object], ...]:
    import json

    payload = json.loads(
        _api(f"repos/{_REPO}/actions/runs/{run_id}/artifacts?per_page=100", run_process)
    )
    items = payload.get("artifacts") if is_object_dict(payload) else None
    if not is_object_list(items):
        return ()
    return tuple(item for item in items if is_object_dict(item))


# meiva:note
# The failing cases a job's JUnit artifact names. The artifact is matched by prefix against the name
# derived from the job, and the longest match wins: a job whose name does not reproduce an artifact
# name gets nothing rather than somebody else's shard, because naming a case from the wrong shard is
# worse than admitting the watch cannot name it.
def junit_case_ids(
    run_id: int, job_name: str, scratch: Path, run_process: RunProcess
) -> tuple[tuple[str, ...], ArtifactMatch]:
    by_name: dict[str, int] = {}
    for artifact in _run_artifacts(run_id, run_process):
        name = artifact.get("name")
        identifier = artifact.get("id")
        if isinstance(name, str) and isinstance(identifier, int):
            by_name[name] = identifier
    chosen = match_artifact(job_name, tuple(by_name))
    if chosen.name is None:
        return (), chosen
    return _artifact_case_ids(by_name[chosen.name], scratch, run_process), chosen


# meiva:note
# The zip lands under the schedule's own run directory and is removed as soon as it is read: the
# path is chosen by the caller (`watch_round` knows the home), not invented here, because a module
# that makes up a temporary name reads the world instead of taking it as an argument.
def _artifact_case_ids(artifact_id: int, scratch: Path, run_process: RunProcess) -> tuple[str, ...]:
    scratch.mkdir(parents=True, exist_ok=True)
    bundle_path = scratch / f"artifact-{artifact_id}.zip"
    try:
        with bundle_path.open("wb") as sink:
            done = run_process(
                ["gh", "api", f"repos/{_REPO}/actions/artifacts/{artifact_id}/zip"],
                stdout=sink,
                stderr=subprocess.PIPE,
                timeout=180,
            )
        if done.returncode != 0:
            return ()
        try:
            with zipfile.ZipFile(bundle_path) as bundle:
                documents = [
                    bundle.read(entry) for entry in bundle.namelist() if entry.endswith(".xml")
                ]
        except (zipfile.BadZipFile, OSError):
            return ()
    finally:
        bundle_path.unlink(missing_ok=True)
    found: list[str] = []
    for raw in documents:
        found.extend(failing_case_ids(raw.decode("utf-8", "replace")))
    return tuple(dict.fromkeys(found))


# meiva:note
# `seen_at`/`seen_attempt` are the round's watermark, and they are applied here rather than only on
# the way out: reading a run costs a jobs listing and one log download per failing job, and a round
# that paid for runs it had already read would spend the difference every thirty minutes (task
# #6509). It is also what keeps one broken run from blinding the stream -- a run whose jobs endpoint
# answers 502 fails only the round that first had to read it, and the rounds after that skip it.
# meiva:note
# The signature is the port's, not the callee's: `watch_once` calls `fetch(workflow, seen_at,
# seen_attempt)` positionally, and the composition binds the two effect parameters by keyword
# through `partial`. Putting `run_process`/`scratch` in front made every call a TypeError --
# "multiple values for argument 'run_process'" -- which the round reported as four unreadable
# streams for six hours on 2026-11-08 (the first wave that carried the watermark, #8677). The
# two halves were each tested; the binding between them was not.
def fetch_runs(
    workflow: str,
    seen_at: datetime | None = None,
    seen_attempt: int | None = None,
    *,
    run_process: RunProcess,
    scratch: Path,
) -> tuple[RunFacts, ...]:
    import json

    listing = json.loads(_api(_runs_path(workflow, 10), run_process))
    definitions: dict[str, str] = {}
    facts: list[RunFacts] = []
    for item in listing.get("workflow_runs", []):
        conclusion = item.get("conclusion")
        if conclusion not in {"success", "failure"}:
            continue
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        # meiva:note
        # The boundary comes from the same two fields the round compares, through the same judgement
        # (`already_read`), so a run this skips is exactly a run the round would have skipped after
        # paying for it.
        if already_read(created, listing_attempt(item), seen_at, seen_attempt):
            continue
        failures: tuple[Failure, ...] = ()
        job_names: tuple[str, ...] = ()
        downstream: tuple[str, ...] = ()
        if conclusion == "failure":
            jobs = json.loads(
                _api(f"repos/{_REPO}/actions/runs/{item['id']}/jobs?per_page=100", run_process)
            ).get("jobs", [])
            failed = [job for job in jobs if job.get("conclusion") == "failure"]
            job_names = tuple(job.get("name", "") for job in failed)
            parsed: list[Failure] = []
            check_steps: frozenset[str] = (
                GATE_CHECK_STEPS if workflow == _MAIN_PUSH else frozenset()
            )
            for job in failed:
                name = job.get("name", "")
                parsed.extend(
                    # meiva:note
                    # The coverage flag travels with the failure, not only with the run: a job that
                    # runs part of the tree reports reds no other job in the workflow can retire by
                    # passing (`_PARTIAL_JOBS`).
                    replace(failure, covers_full_suite=name not in _PARTIAL_JOBS)
                    for failure in _job_failures(
                        job,
                        run_id=int(item.get("id") or 0),
                        check_steps=check_steps,
                        scratch=scratch,
                        run_process=run_process,
                    )
                )
            failures = tuple(parsed)
            sha = item.get("head_sha", "")
            if sha not in definitions:
                definitions[sha] = _workflow_text(workflow, sha, run_process)
            if definitions[sha]:
                downstream = downstream_failing_jobs(definitions[sha], job_names)
        facts.append(
            RunFacts(
                workflow=workflow,
                head_sha=item.get("head_sha", ""),
                created_at=created,
                conclusion=conclusion,
                failures=failures,
                html_url=item.get("html_url"),
                job_names=job_names,
                downstream_jobs=downstream,
                covers_full_suite=workflow in _FULL_SUITE_WORKFLOWS,
                run_id=int(item.get("id") or 0),
                attempt=listing_attempt(item),
                head_title=_commit_subject(item.get("head_sha", ""), run_process)
                if conclusion == "failure"
                else "",
            )
        )
    return tuple(facts)


# meiva:note
# One run's newest attempt, as GitHub holds it now rather than as the listing described it: an
# attempt can start or finish between the listing and the card, and the gate must answer about the
# attempt that exists when it is asked. Two reads, because the attempt number is a field of the run
# and one attempt's jobs are listed under it. The attempt number is parsed by the same helper the
# listing uses, so the two cannot drift.
def latest_attempt_jobs(
    run_id: int, run_process: RunProcess
) -> tuple[int, tuple[Mapping[str, object], ...]]:
    import json

    run = json.loads(_api(f"repos/{_REPO}/actions/runs/{run_id}", run_process))
    attempt = listing_attempt(run) if is_object_dict(run) else 1
    payload = json.loads(
        _api(
            f"repos/{_REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
            run_process,
        )
    )
    jobs = payload.get("jobs") if is_object_dict(payload) else None
    if not is_object_list(jobs):
        return attempt, ()
    return attempt, tuple(job for job in jobs if is_object_dict(job))


# meiva:note
# The merge commit's subject, so a card names the change instead of only a sha. Read for a failing
# run alone: a green run's subject is not evidence anyone acts on.
def _commit_subject(sha: str, run_process: RunProcess) -> str:
    if not sha:
        return ""
    result = run_process(
        ["gh", "api", f"repos/{_REPO}/commits/{sha}", "--jq", ".commit.message"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip().splitlines()[0][:120] if result.stdout.strip() else ""


# meiva:note
# The commits between the last green and the red, newest first, which is the list the card's heading
# promises. The compare answers oldest-first, so the reversal here is what makes the truncation in
# `card_body` keep the newest: the change that broke the run is the last one before the red.
def compare_commits(base: str, head: str, run_process: RunProcess) -> tuple[str, ...]:
    import json

    payload = json.loads(_api(f"repos/{_REPO}/compare/{base}...{head}", run_process))
    commits = payload.get("commits")
    if not is_object_list(commits):
        return ()
    lines: list[str] = []
    for commit in commits:
        if not is_object_dict(commit):
            continue
        sha = commit.get("sha")
        inner = commit.get("commit")
        message = inner.get("message", "") if is_object_dict(inner) else ""
        if not isinstance(sha, str) or not sha:
            continue
        subject = str(message).split("\n", 1)[0].strip()
        lines.append(f"{sha[:10]} {subject}".strip())
    return tuple(reversed(lines))


def _last_run_start(workflow: str, run_process: RunProcess) -> datetime | None:
    import json

    listing = json.loads(_api(_runs_path(workflow, 1), run_process))
    runs = listing.get("workflow_runs", [])
    if not runs:
        return None
    return datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))


# meiva:note
# The slot check reads the cron's own runs, not the workflow's newest run: a make-up dispatch is a
# `workflow_dispatch` run, and counting it would let the repair hide the outage it repaired. The
# whole list comes back because the card states how many arrivals a day should have contained --
# that count is what a reader can check against GitHub, so it has to come from the same read.
def _schedule_starts(workflow: str, run_process: RunProcess) -> tuple[datetime, ...]:
    import json

    listing = json.loads(_api(_runs_path(workflow, 20), run_process))
    starts = [
        datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
        for run in listing.get("workflow_runs", [])
        if run.get("event") == "schedule" and isinstance(run.get("created_at"), str)
    ]
    return tuple(sorted(starts, reverse=True))


# meiva:note
# The slot leg fails on its own. Its two reads -- the cron's own starts and whether the workflow is
# busy -- used to sit in the argument list of `slot_round`, so one bad answer took the whole round
# with it: `fire` has no catch above it, and a round that raises costs the heartbeat the supervisor
# reads. A failure here becomes one named note, and the round still reports the reds it did read.
#
# The leg is skipped, not run with `last_schedule_start=None`: that value means "this workflow
# never started here", and a card built from it would report a stopped cron on the strength of a
# failed read -- the exact shape of the false alarm this watch already had once.
def _slot_note(
    *,
    state_path: Path,
    uncertified: int | None,
    channel_latest: str | None,
    run_process: RunProcess,
    report_agent: int,
) -> str | None:
    try:
        starts = _schedule_starts(_RELEASE_GATE, run_process)
        busy = _workflow_busy(_RELEASE_GATE, run_process)
    except Exception as exc:
        return slot_round(
            state_path=state_path,
            now=datetime.now(UTC),
            facts=None,
            open_card=partial(_open_card, report_agent=report_agent),
            close_card=_close_card,
            unreadable=f"{type(exc).__name__}: {exc}",
        )
    return slot_round(
        state_path=state_path,
        now=datetime.now(UTC),
        facts=SlotFacts(
            last_schedule_start=starts[0] if starts else None,
            busy=busy,
            uncertified_commits=uncertified,
            channel_latest=channel_latest,
            scheduled_starts=starts,
            cadence=RELEASE_GATE_SLOT_CADENCE,
        ),
        open_card=partial(_open_card, report_agent=report_agent),
        close_card=_close_card,
    )


# meiva:note
# A workflow that is queued or running is about to say something, so its slot is not missing yet:
# dispatching over it would stack two full suites on the same runner pool.
def _workflow_busy(workflow: str, run_process: RunProcess) -> bool:
    import json

    listing = json.loads(_api(_runs_path(workflow, 20), run_process))
    return any(
        run.get("status") in ("queued", "in_progress") for run in listing.get("workflow_runs", [])
    )


# meiva:note
# How far `channel/latest` trails `main`: the gate skips a slot when nothing is uncertified, and
# that skip is harmless. Reading the distance is what separates a harmless skip from a gap that
# leaves commits unchecked. An unreadable compare returns None, and the card then says so rather
# than claiming there is work waiting.
def _uncertified_commits(run_process: RunProcess) -> tuple[int | None, str | None]:
    import json

    try:
        payload = json.loads(
            _api(f"repos/{_REPO}/compare/channel/latest...main?per_page=1", run_process)
        )
    except RuntimeError:
        return None, None
    if not is_object_dict(payload):
        return None, None
    latest = payload.get("base_commit")
    ahead = payload.get("ahead_by")
    sha = latest.get("sha") if is_object_dict(latest) else None
    return (ahead if isinstance(ahead, int) else None), (sha if isinstance(sha, str) else None)


def _open_card(card: Card, report_agent: int) -> int:
    return int(
        task_registry.create_in_context(
            meiva.context,
            title=card.title,
            description=card.body,
            parent=1,
            owner=report_agent,
            priority="P1",
            operation_key=card_operation_key(card),
        ).id
    )


# meiva:note
# The liveness card closes by title because a closed card is a statement about the watch, not about
# one occurrence of it: whichever round lands again answers the report, and `_open_card` has
# already made the next stale period a card of its own.
def _close_liveness_card(report_agent: int) -> None:
    for task in meiva.tasks.list(owner=report_agent, status="in_progress"):
        if task.title == STALE_WATCH_TITLE:
            task_registry.update_in_context(
                meiva.context,
                task.id,
                status="done",
                results="watch reporting again: a round landed",
                operation_key=f"main-red-close-{task.id}",
            )


# meiva:note
# The one-shot notice the watch owes whoever acts on its cards: when it takes its first suspect, the
# question of whether that suspect can ever be re-checked becomes real, and the person who would act
# on the answer hears it once (405, 2026-11-08). Every later round already has a suspect and stays
# quiet -- this is a transition, not a status.
def _notify_lead(text: str, report_agent: int) -> None:
    meiva.agents.send_message(report_agent, text)


def _close_card(task_id: int, reason: str) -> None:
    task_registry.update_in_context(
        meiva.context,
        task_id,
        status="done",
        results=reason,
        operation_key=f"main-red-close-{task_id}",
    )


def watch_round(
    slot: datetime, _payload: object, *, inputs: ScheduleInputs, home: Path, report_agent: int
) -> None:
    state_path = home / "run" / STATE_FILE
    run_process = subprocess.run
    line = watch_once(
        state_path=state_path,
        now=datetime.now(UTC),
        fetch=partial(
            fetch_runs, run_process=run_process, scratch=home / "run" / "main-red-artifacts"
        ),
        last_start=partial(_last_run_start, run_process=run_process),
        open_card=partial(_open_card, report_agent=report_agent),
        close_card=_close_card,
        notify=partial(_notify_lead, report_agent=report_agent),
        emit=lambda payload: emit_system_event(
            _EVENT_NAME,
            payload,
            process=_PROCESS_NAME,
            producer=inputs.producer,
            machine_reader=inputs.machine.name,
            image=inputs.image,
            home=inputs.home,
        ),
        workflows=(_RELEASE_GATE, _NIGHTLY, _CI_NIGHTLY, _MAIN_PUSH),
        compare=partial(compare_commits, run_process=run_process),
        attempt_of=partial(latest_attempt_jobs, run_process=run_process),
        blind_min_streams=settings.daemon.main_red_watch_blind_streams,
        blind_stream_rounds=settings.daemon.main_red_watch_blind_stream_rounds,
        liveness=(
            LivenessStream("nightly", _NIGHTLY, NIGHTLY_STALE_AFTER),
            LivenessStream(
                "nightly-full",
                _CI_NIGHTLY,
                NIGHTLY_STALE_AFTER,
                make_up=(
                    "`gh workflow run ci.yml --ref main` (full suite, publishes nothing, about ten "
                    "to fifteen minutes)."
                ),
            ),
        ),
    )
    uncertified, channel_latest = _uncertified_commits(run_process)
    slot_note = _slot_note(
        state_path=state_path,
        uncertified=uncertified,
        channel_latest=channel_latest,
        run_process=run_process,
        report_agent=report_agent,
    )
    if slot_note is not None:
        line = f"{line} | {slot_note}"
    print(f"[{datetime.now(UTC).isoformat()}] {line}", flush=True)


# meiva:note
# The liveness trigger is five minutes past each round: far enough that a slow round has finished
# and printed, close enough that a stopped watch is reported inside the hour it went quiet.
def liveness_tick(
    slot: datetime, _payload: object, *, inputs: ScheduleInputs, home: Path, report_agent: int
) -> None:
    del home
    liveness_once(
        slot,
        database=inputs.database(),
        open_card=partial(_open_card, report_agent=report_agent),
        close_card=partial(_close_liveness_card, report_agent),
    )


def dispatch(
    slot: datetime, payload: object, *, inputs: ScheduleInputs, home: Path, report_agent: int
) -> None:
    if payload == _LIVENESS_PAYLOAD:
        liveness_tick(slot, payload, inputs=inputs, home=home, report_agent=report_agent)
        return
    watch_round(slot, payload, inputs=inputs, home=home, report_agent=report_agent)


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        round_once = partial(
            dispatch,
            inputs=inputs,
            home=home,
            report_agent=report_agent_from_environ(os.environ),
        )
        if "--once" in sys.argv:
            round_once(datetime.now(UTC), None)
            raise SystemExit(0)
        run_loop(
            inputs.database(),
            CronSchedule(
                (Trigger(CRON), Trigger(LIVENESS_CRON, _LIVENESS_PAYLOAD)),
                timezone=cluster_timezone(),
            ),
            round_once,
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            home=inputs.home,
            started_at=datetime.now(UTC),
            clock=lambda: datetime.now(UTC),
        )
