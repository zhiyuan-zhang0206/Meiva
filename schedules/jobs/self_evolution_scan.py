# meiva:note
# Run the daily self-evolution scan and act on what it finds.
#
# `daily_scan.py` ships with the meiva-self-evolution skill and the entry points at the skill's
# load-directory copy: converge-managed but bootstrap-only (R5), refreshed to the landed
# revision by the rollout's update legs, with `meiva skill update` as the manual way. The
# bound separates a slow scan from a wedged one: a measured full day is about 113 minutes with
# two collect streams running (2026-09-25), and the timeout is roughly 1.6x that.
# meiva:link schedules/docs/self-evolution.meiva.okf.md

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import meiva
from schedules.jobs.agent_directory import ensure_agent
from schedules.plans.scan_outcome import ScanMissing, ScanRan, ScanResult, ScanTimedOut, plan_scan

__all__ = ["AGENT_LABEL", "DailyScan", "run_daily_scan"]

AGENT_LABEL = "self-evolution"


# meiva:note
# `command` is the full argv of the scan (built by the entry from the skill's path), `script`
# the file it runs: an absent script is reported instead of run, because python exits 2 when
# it cannot open it and that would read as an ALERT.
@dataclass(frozen=True)
class DailyScan:
    command: tuple[str, ...]
    script: Path
    timeout: int
    report_agent: int | None


def _scan(scan: DailyScan) -> ScanResult:
    if not scan.script.is_file():
        return ScanMissing(str(scan.script))
    try:
        done = subprocess.run(
            scan.command,
            timeout=scan.timeout,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ScanTimedOut(scan.timeout)
    return ScanRan(done.returncode, done.stdout or "", done.stderr or "")


def run_daily_scan(slot: datetime, _payload: str | None, *, scan: DailyScan) -> None:
    plan = plan_scan(_scan(scan), slot)
    for line in plan.log_lines:
        print(line)
    if plan.wake_prompt is not None:
        ensure_agent(AGENT_LABEL, plan.wake_prompt)
    if plan.daily_report is not None and scan.report_agent is not None:
        try:
            meiva.agents.send_message(scan.report_agent, plan.daily_report)
        except Exception as exc:
            print(f"daily report to {scan.report_agent} failed: {exc}")
