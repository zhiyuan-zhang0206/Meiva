# meiva:note
# Run the upstream model checker and tell the P0 lead what it found.
#
# The checker pulls each provider's official model list, diffs the registry and writes
# `last-report.json` beside its markdown report. Exit 0 is quiet, exit 2 means new candidates,
# anything else (or a run that never finished) is a failure the lead hears about. The entry
# builds the command, working directory and child environment; this runs them.
# meiva:link schedules/README.md

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import assert_never

from schedules.jobs.agent_directory import notify_report_agent
from schedules.plans.directory_plan import ReportRecipient
from schedules.plans.tracker_report import (
    Failed,
    Quiet,
    ReadCandidates,
    candidate_message,
    failure_message,
    plan_tracker_exit,
)

__all__ = ["ModelTracker", "run_tracker"]


@dataclass(frozen=True)
class ModelTracker:
    command: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    report_dir: Path
    timeout: float
    recipient: ReportRecipient


def run_tracker(_slot: datetime, _payload: str | None, *, tracker: ModelTracker) -> None:
    try:
        done = subprocess.run(
            tracker.command,
            cwd=tracker.cwd,
            env=dict(tracker.env),
            capture_output=True,
            text=True,
            timeout=tracker.timeout,
            check=False,
        )
        verdict = plan_tracker_exit(done.returncode, done.stdout + done.stderr)
        match verdict:
            case Quiet():
                print(f"[{datetime.now(UTC).isoformat()}] model update tracker: no new candidates")
            case ReadCandidates():
                report = json.loads((tracker.report_dir / "last-report.json").read_text())
                message = candidate_message(report, str(tracker.report_dir / "last-report.md"))
                notify_report_agent(tracker.recipient, message)
            case Failed(message=message):
                notify_report_agent(tracker.recipient, message)
            case _:
                assert_never(verdict)
    except Exception as exc:
        notify_report_agent(tracker.recipient, failure_message(f"subprocess exception: {exc}"))
