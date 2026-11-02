# meiva:note
# The trunk-red watch: every thirty minutes, read the newest post-merge runs of `main` and turn a
# red that no green run explains into one card for the P0 lead.
#
# It exists because the queue that used to verify a merged tree is gone (retired 2026-11-01) and
# `ci.yml` never runs on a push to `main`: the six-hourly `release-gate` and the nightly verdict
# are the only evidence that `main` is green, and on 2026-11-01 both scheduled runs of the gate
# went red without anyone seeing them. A red page is not a signal nobody is watching for; it is
# no signal at all.
#
# The decision lives in `schedules/plans/trunk_red.py` and the effects in
# `schedules/jobs/trunk_red_watch.py`; this entry only supplies the cluster's own values.
# meiva:link schedules/README.md

import os
import subprocess
import sys
from datetime import UTC, datetime
from functools import partial

import meiva
from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import cluster_timezone, open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.jobs.watch.trunk_red_watch import STATE_FILE, watch_once
from schedules.jobs.telemetry_event import emit_system_event
from schedules.plans.schedule_identity import parse_schedule_id

CRON = "*/30 * * * *"
_PROCESS_NAME = "trunk-red-watch"
_EVENT_NAME = "trunk_red_watch_round"
_RELEASE_GATE = "release-gate.yml"
_NIGHTLY = "refresh-test-durations.yml"
_REPORT_AGENT = 405
_ENV_REPORT_AGENT = "MEIVA_TRUNK_RED_REPORT_AGENT"
_REPO = "zhiyuan-zhang0206/Meiva"


# meiva:note
# The lead the cards belong to. A site that reports elsewhere sets the variable; a value that is
# not a positive id stops the round naming the variable instead of opening cards into nothing.
def report_agent_from_environ(environ):
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


def _api(path, run_process):
    result = run_process(
        ["gh", "api", "--method", "GET", path], capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {(result.stderr or '').strip()[-300:]}")
    return result.stdout


def _job_log(job_id, run_process):
    result = run_process(
        ["gh", "api", "--method", "GET", f"repos/{_REPO}/actions/jobs/{job_id}/logs"],
        capture_output=True,
        text=True,
        timeout=180,
    )
    return result.stdout if result.returncode == 0 else ""


def fetch_runs(workflow, run_process):
    import json

    from schedules.plans.watch.trunk_red import RunFacts, parse_failures

    listing = json.loads(
        _api(f"repos/{_REPO}/actions/workflows/{workflow}/runs?per_page=10", run_process)
    )
    facts = []
    for item in listing.get("workflow_runs", []):
        conclusion = item.get("conclusion")
        if conclusion not in {"success", "failure"}:
            continue
        failures = ()
        job_names = ()
        if conclusion == "failure":
            jobs = json.loads(
                _api(f"repos/{_REPO}/actions/runs/{item['id']}/jobs?per_page=100", run_process)
            ).get("jobs", [])
            failed = [job for job in jobs if job.get("conclusion") == "failure"]
            job_names = tuple(job.get("name", "") for job in failed)
            parsed = []
            for job in failed:
                log = _job_log(job["id"], run_process)
                parsed.extend(
                    parse_failures(
                        log[-400_000:],
                        job_name=job.get("name", ""),
                        suite_step_failed=True,
                    )
                )
            failures = tuple(parsed)
        facts.append(
            RunFacts(
                workflow=workflow,
                head_sha=item.get("head_sha", ""),
                created_at=datetime.fromisoformat(item["created_at"].replace("Z", "+00:00")),
                conclusion=conclusion,
                failures=failures,
                html_url=item.get("html_url"),
                job_names=job_names,
            )
        )
    return tuple(facts)


def _last_run_start(workflow, run_process):
    import json

    listing = json.loads(
        _api(f"repos/{_REPO}/actions/workflows/{workflow}/runs?per_page=1", run_process)
    )
    runs = listing.get("workflow_runs", [])
    if not runs:
        return None
    return datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))


def _open_card(card, report_agent):
    return int(
        meiva.tasks.create(
            title=card.title,
            description=card.body,
            parent=1,
            owner=report_agent,
            priority="P1",
            operation_key=f"trunk-red-{card.fingerprint}",
        ).id
    )


def _close_card(task_id, reason):
    meiva.tasks.update(
        task_id,
        status="done",
        results=f"red ended: {reason}",
        operation_key=f"trunk-red-close-{task_id}",
    )


def watch_round(slot, _payload, *, inputs, home, report_agent):
    state_path = home / "run" / STATE_FILE
    run_process = subprocess.run
    line = watch_once(
        state_path=state_path,
        now=datetime.now(UTC),
        fetch=partial(fetch_runs, run_process=run_process),
        open_card=partial(_open_card, report_agent=report_agent),
        close_card=_close_card,
        emit=lambda payload: emit_system_event(
            _EVENT_NAME,
            payload,
            process=_PROCESS_NAME,
            producer=inputs.producer,
            machine_reader=inputs.machine.name,
            image=inputs.image,
            home=inputs.home,
        ),
        workflows=(_RELEASE_GATE, _NIGHTLY),
        release_gate_last_start=_last_run_start(_RELEASE_GATE, run_process),
        nightly_last_start=_last_run_start(_NIGHTLY, run_process),
    )
    print(f"[{datetime.now(UTC).isoformat()}] {line}", flush=True)


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        round_once = partial(
            watch_round,
            inputs=inputs,
            home=home,
            report_agent=report_agent_from_environ(os.environ),
        )
        if "--once" in sys.argv:
            round_once(datetime.now(UTC), None)
            raise SystemExit(0)
        run_loop(
            inputs.database(),
            CronSchedule((Trigger(CRON),), timezone=cluster_timezone()),
            round_once,
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )
