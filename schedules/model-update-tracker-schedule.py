# meiva:link schedules/README.md

import os
import sys

from base.paths import repo_root
from base.paths.home import resolve_meiva_home
from schedules.jobs.entry import open_schedule_entry
from schedules.jobs.model_tracker import ModelTracker, run_tracker
from schedules.plans.directory_plan import report_recipient
from schedules.plans.schedule_identity import parse_scheduled_for

CRON = "0 6 * * *"
_REPORT_LABEL = "Meiva \u8d1f\u8d23\u4eba"
_REPORT_AGENT_ENV = "MEIVA_MODEL_UPDATE_REPORT_AGENT"
_TIMEOUT_SECONDS = 120


if __name__ == "__main__":
    home = resolve_meiva_home()
    with open_schedule_entry(globals().get("MEIVA_SCHEDULE_INPUTS"), home=home) as inputs:
        root = repo_root()
        report_dir = home / "model-tracker"
        tracker = ModelTracker(
            command=(
                sys.executable,
                "scripts/model_registry/check_model_updates.py",
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
        run_tracker(
            parse_scheduled_for(os.environ.get("MEIVA_SCHEDULED_FOR")),
            None,
            tracker=tracker,
        )
