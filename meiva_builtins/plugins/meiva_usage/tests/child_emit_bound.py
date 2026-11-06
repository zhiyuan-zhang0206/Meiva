# meiva:note
# A schedule entry that can hand over its pipeline, and a process that runs the collector loop
# twice: two runs in one process is what proves the bind is per process, not per run —
# `init_telemetry` refuses a second live pipeline, so a binder without that guard raises here
# (2026-11-06, task #6352). The pipeline's writer is a list, so the events this child reports are
# the events `emit()` actually enqueued.
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, cast

from base.daemon.schedules.inputs import ScheduleInputs
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.delivery.pipeline import EventPipeline
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig, UsageConfig
from meiva_builtins.plugins.meiva_usage.runner import run_collector_cycles

COLLECTOR_ID = "scheduled-collector"

home = Path(tempfile.mkdtemp(prefix="meiva-usage-bound-"))
config_dir = home / "configs" / "meiva_usage"
config_dir.mkdir(parents=True)
(config_dir / "config.json").write_text(
    UsageConfig(
        collectors=(
            CollectorConfig(
                id=COLLECTOR_ID,
                label="Scheduled Collector",
                provider="deepseek",
                machine="company-air",
                key_env="DEEPSEEK_API_KEY",
                interval_minutes=1,
            ),
        )
    ).model_dump_json(indent=2)
)


# meiva:note
# The loop reads the machine name and the database handle from its inputs; a no-op refresh means
# neither is used, and the only thing under test is what the loop does with a cycle it can emit.
class _Machine:
    def name(self) -> str:
        return "company-air"


# meiva:note
# The writer is where an event lands once the drain thread has taken it: a list is the whole
# assertion surface this child needs, and `stop()` is what turns "queued" into "written".
def _no_refresh(*_args: Any, **_kwargs: Any) -> None:
    return None


def _no_sleep(_seconds: float) -> None:
    return None


written: list[Any] = []
pipeline = EventPipeline(writer=written.extend)


def _pipeline() -> EventPipeline:
    return pipeline


inputs = ScheduleInputs(
    database=cast("Any", lambda: None),
    producer=cast("Any", _pipeline),
    image=LoadedCommit(source_root=home, sha="test-sha"),
    machine=cast("Any", _Machine()),
    home=home,
)

for _run in range(2):
    run_collector_cycles(
        inputs,
        refresh=_no_refresh,
        sleep=_no_sleep,
        collector_id=COLLECTOR_ID,
        stop_after_cycles=2,
        environ={"DEEPSEEK_API_KEY": "test-key-123"},
    )

pipeline.stop(timeout=5)

cycles = [
    {
        "collector": event.attributes.get("collector"),
        "schedule_id": event.attributes.get("schedule_id"),
        "cycle": event.attributes.get("cycle"),
        "process": event.process,
    }
    for event in written
    if event.event_name == "plugin_collector_cycle"
]
print(
    json.dumps(
        {
            "cycles": cycles,
            "schedule_id_env": os.environ.get("MEIVA_SCHEDULE_ID"),
            "other_events": sorted(
                {event.event_name for event in written} - {"plugin_collector_cycle"}
            ),
        }
    )
)
