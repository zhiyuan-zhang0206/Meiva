# meiva:link schedules/README.md

import os
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import base
from base.paths.home import resolve_meiva_home
from schedules.jobs.catchup import cluster_timezone
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.loop import CronSchedule, Trigger, run_loop
from schedules.jobs.model_tracker import ModelTracker, run_tracker
from schedules.plans.directory_plan import report_recipient
from schedules.plans.schedule_identity import parse_schedule_id

CRON = "0 6 * * *"
_REPORT_LABEL = "Meiva \u8d1f\u8d23\u4eba"
_REPORT_AGENT_ENV = "MEIVA_MODEL_UPDATE_REPORT_AGENT"
_TIMEOUT_SECONDS = 120


if __name__ == "__main__":
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS")) as inputs:
        # meiva:note
        # `base.__file__` rather than `repo_root()`: the same directory, expressed so the checker can
        # see the file this schedule runs (`python-commands` reads a literal path or `-m <module>`
        # after the interpreter). The call form was a recorded `restricted-python` debt; this spelling
        # pays it down and keeps the schedule running the tracked copy.
        root = Path(base.__file__).resolve().parents[1]
        report_dir = resolve_meiva_home() / "model-tracker"
        tracker = ModelTracker(
            command=(
                sys.executable,
                str(root / "scripts" / "model_registry" / "check_model_updates.py"),
                "--write-report",
                str(report_dir),
            ),
            cwd=root,
            env={**os.environ, "PYTHONPATH": str(root)},
            report_dir=report_dir,
            timeout=_TIMEOUT_SECONDS,
            recipient=report_recipient(
                "model-update-tracker",
                _REPORT_AGENT_ENV,
                _REPORT_LABEL,
                os.environ.get(_REPORT_AGENT_ENV),
            ),
        )
        run_loop(
            inputs.database(),
            CronSchedule((Trigger(CRON),), timezone=cluster_timezone()),
            partial(run_tracker, tracker=tracker),
            schedule_id=parse_schedule_id(os.environ.get("MEIVA_SCHEDULE_ID")),
            started_at=datetime.now(UTC),
        )
