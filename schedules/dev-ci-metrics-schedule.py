from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo

import meiva
import base
from meiva.agents import AgentStatus as S
from schedules.agent_status_guard import ensure_agent_status_members
from schedules.catchup import cluster_timezone
from schedules.daily_host import report_agent, run_daily_loop
from base.daemon.schedules.inputs import ScheduleInputs
from schedules.entry import fresh_schedule_entry, schedule_entry


CRON = "20 6 * * *"
_REPORT_AGENT_ENV = "MEIVA_CI_METRICS_REPORT_AGENT"
_REPORT_LABEL = "Meiva \u8d1f\u8d23\u4eba"
_REPO_ROOT = Path(base.__file__).resolve().parents[1]
# meiva:note
# Timed workflows do not block a pull request, so a red one is silent by default: the
# nightly duration refresh failed 6 of 8 runs and nothing said so (task #5479), and the
# pricing refresh failed 10 in a row the same way (task #5496). Two consecutive failures
# is already "this pipeline is broken", not "one bad night". Each entry carries what
# stays wrong while it is red, because the alert has to say what it costs.
_SCHEDULED_WORKFLOWS = {
    "refresh-test-durations.yml": (
        "CI keeps splitting shards with the previous test-duration model"
    ),
    "update-model-pricing.yml": (
        "the price catalog stops following its sources, so every per-model cost figure "
        "drifts from what the vendor charges"
    ),
}
_CONSECUTIVE_FAILURES_ALERT = 2


def _report_agent() -> int:
    return report_agent(_REPORT_AGENT_ENV, _REPORT_LABEL)


def _report_failure(detail: str) -> None:
    message = (
        f"Dev/CI metrics collection failed:\n{detail[-1000:]}\n"
        "Check the schedule log; backfill with `scripts/ci/pull_requests/runs_export.py --repo "
        "zhiyuan-zhang0206/Meiva --print-snapshot`."
    )
    try:
        meiva.agents.send_message(_report_agent(), message)
    except Exception as exc:
        print(f"dev-ci-metrics could not notify the P0 lead: {exc}")
        raise


def _load_exporter() -> Any:
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from scripts.ci.pull_requests import runs_export

    return runs_export


def _snapshot(exporter: Any) -> dict[str, Any]:
    from base.paths import meiva_home

    path = meiva_home() / exporter._STATE_DIR_RELATIVE / "snapshot.json"
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError("CI-run sampler wrote an invalid snapshot")
    return loaded


def _day_counts(snapshot: dict[str, Any], day: str, repo: str) -> tuple[int, int]:
    records = snapshot["repositories"][repo]["days"]
    values = records[day]
    return int(values["runs"]), int(values["failed_runs"])


def _run_exporter(exporter: Any, repo: str) -> None:
    if exporter.main(["--repo", repo], producer=meiva.context.clients.event_pipeline) != 0:
        raise RuntimeError("ci-runs-export exited non-zero")


def _workflow_runs(
    exporter: Any, repo: str, workflow: str, limit: int = 10
) -> list[dict[str, Any]]:
    payload = exporter._run_gh(
        [f"repos/{repo}/actions/workflows/{workflow}/runs?per_page={limit}"],
        exporter.FetchStats(),
    )
    runs = json.loads(payload).get("workflow_runs")
    if not isinstance(runs, list):
        raise TypeError(f"{workflow}: the runs listing carried no workflow_runs")
    return runs


# meiva:note
# Newest first: a streak ends at the first run that did not fail.
def consecutive_failures(runs: list[dict[str, Any]]) -> int:
    streak = 0
    for run in runs:
        if run.get("conclusion") != "failure":
            break
        streak += 1
    return streak


# meiva:note
# The one outbound call, injected by tests: patching meiva.agents from a test hides
# what it replaces from the locality checker.
def _message_p0_lead(message: str) -> None:
    meiva.agents.send_message(_report_agent(), message)


def _alert_broken_scheduled_workflows(
    exporter: Any, repo: str, *, notify: Callable[[str], None] = _message_p0_lead
) -> None:
    for workflow, cost in _SCHEDULED_WORKFLOWS.items():
        try:
            runs = _workflow_runs(exporter, repo, workflow)
        except Exception as exc:
            print(f"dev-ci-metrics: {workflow} runs unreadable: {type(exc).__name__}: {exc}")
            continue
        streak = consecutive_failures(runs)
        if streak < _CONSECUTIVE_FAILURES_ALERT:
            continue
        newest = runs[0]
        print(f"dev-ci-metrics: {workflow} failed {streak} runs in a row")
        notify(
            f"Scheduled workflow {workflow} failed {streak} runs in a row. It does not "
            "block a pull request, so a red run is silent by default: while it keeps "
            f"failing, {cost}.\n"
            f"Newest run: {newest.get('html_url')} ({newest.get('created_at')})\n"
            "Find the failing step; the fix is accepted only after two consecutive "
            "successful nights.",
        )


def _fire(slot_end: datetime, _payload: None) -> None:
    try:
        exporter = _load_exporter()
        repo = exporter.DEFAULT_REPO
        _run_exporter(exporter, repo)
        day = (
            slot_end.astimezone(ZoneInfo(cluster_timezone())).date() - timedelta(days=1)
        ).isoformat()
        runs, failed = _day_counts(_snapshot(exporter), day, repo)
        _alert_broken_scheduled_workflows(exporter, repo)
        print(
            f"[{datetime.now(UTC).isoformat()}] dev-ci-metrics: {day} — {runs} runs, {failed} failed"
        )
    except Exception as exc:
        print(f"dev-ci-metrics failed: {exc}")
        _report_failure(f"{type(exc).__name__}: {exc}")


def _main_loop(*, inputs: ScheduleInputs) -> None:
    db = inputs.database()
    run_daily_loop(db, CRON, cluster_timezone(), _fire)


if __name__ == "__main__":
    ensure_agent_status_members(
        S, {"IDLING", "RUNNING", "TERMINATED"}, schedule_name="dev-ci-metrics"
    )
    entry_inputs = globals().get("MEIVA_SCHEDULE_INPUTS")
    if entry_inputs is None:
        from base.log import prepare_process_logging

        prepare_process_logging()
    with fresh_schedule_entry() if entry_inputs is None else schedule_entry(entry_inputs) as inputs:
        _main_loop(inputs=inputs)
