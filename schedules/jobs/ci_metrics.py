# meiva:note
# The daily Dev/CI metrics run: sample GitHub Actions through the exporter, read back the
# previous complete day, and wake the P0 lead when a sampling run fails or a timed workflow has
# been red twice in a row.
#
# The exporter and its GitHub transport are the CI scripts' own (`runs_export`, `runs_api`),
# imported where they run: the schedule does not depend on them until it fires. The exporter is
# handed the state directory explicitly so the snapshot this reads back is the one it wrote.
# meiva:link schedules/README.md

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from base.daemon.schedules.inputs import ScheduleInputs
from schedules.jobs.agent_directory import notify_report_agent
from schedules.plans.ci_workflows import (
    WatchedWorkflow,
    day_counts,
    failure_message,
    parse_workflow_runs,
    plan_alert,
    previous_day,
)
from schedules.plans.directory_plan import ReportRecipient

if TYPE_CHECKING:
    from base.clock import Timers
    from scripts.ci.pull_requests.export_process import GhProcess

__all__ = ["CiMetrics", "alert_broken_workflows", "collect_ci_metrics"]


# meiva:note
# What one metrics run needs, read once by the entry. `run_process` and `timers` are the
# exporter's own required transport; `state_dir` is where it keeps its cache and snapshot.
@dataclass(frozen=True)
class CiMetrics:
    tz: str
    recipient: ReportRecipient
    watched: tuple[WatchedWorkflow, ...]
    state_dir: Path
    run_process: GhProcess
    timers: Timers


def alert_broken_workflows(repo: str, job: CiMetrics) -> None:
    from scripts.ci.pull_requests import runs_api

    for workflow in job.watched:
        try:
            payload = runs_api.run_gh(
                [f"repos/{repo}/actions/workflows/{workflow.file}/runs?per_page=10"],
                runs_api.FetchStats(),
                run_process=job.run_process,
                timers=job.timers,
            )
            runs = parse_workflow_runs(payload, workflow.file)
        except Exception as exc:
            print(f"dev-ci-metrics: {workflow.file} runs unreadable: {type(exc).__name__}: {exc}")
            continue
        alert = plan_alert(workflow, runs)
        if alert is None:
            continue
        print(f"dev-ci-metrics: {workflow.file} failed {alert.streak} runs in a row")
        notify_report_agent(job.recipient, alert.message)


def _run_exporter(job: CiMetrics, inputs: ScheduleInputs) -> str:
    from scripts.ci.pull_requests import runs_export

    exit_code = runs_export.main(
        ["--repo", runs_export.DEFAULT_REPO, "--state-dir", str(job.state_dir)],
        run_process=job.run_process,
        timers=job.timers,
        producer=inputs.producer,
    )
    if exit_code != 0:
        raise RuntimeError("ci-runs-export exited non-zero")
    return runs_export.DEFAULT_REPO


def collect_ci_metrics(
    slot_end: datetime, _payload: str | None, *, job: CiMetrics, inputs: ScheduleInputs
) -> None:
    try:
        repo = _run_exporter(job, inputs)
        day = previous_day(slot_end, job.tz)
        snapshot = json.loads((job.state_dir / "snapshot.json").read_text(encoding="utf-8"))
        runs, failed = day_counts(snapshot, day, repo)
        alert_broken_workflows(repo, job)
        print(
            f"[{datetime.now(UTC).isoformat()}] dev-ci-metrics: {day} — {runs} runs, {failed} failed"
        )
    except Exception as exc:
        print(f"dev-ci-metrics failed: {exc}")
        notify_report_agent(job.recipient, failure_message(f"{type(exc).__name__}: {exc}"))
