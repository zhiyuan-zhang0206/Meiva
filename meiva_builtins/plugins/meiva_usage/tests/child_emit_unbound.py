# meiva:note
# A schedule runner's process shape: it binds no telemetry pipeline, and it runs the real collector
# loop through the public entry. Two cycles, so a run of this child says both that a cycle event
# that cannot be emitted is reported and that it is reported once.
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, cast

from base.daemon.schedules.inputs import ScheduleInputs
from base.native_process.loaded_commit import LoadedCommit
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig, UsageConfig
from meiva_builtins.plugins.meiva_usage.runner import run_collector_cycles

COLLECTOR_ID = "scheduled-collector"

home = Path(tempfile.mkdtemp(prefix="meiva-usage-unbound-"))
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
# neither is used, and the only thing under test is what the loop does with a cycle it cannot emit.
class _Machine:
    def name(self) -> str:
        return "company-air"


inputs = ScheduleInputs(
    database=cast("Any", lambda: None),
    producer=cast("Any", lambda: None),
    image=LoadedCommit(source_root=home, sha="test-sha"),
    machine=cast("Any", _Machine()),
    home=home,
)


# meiva:note
# The loop's two effects, supplied by the caller: a no-op refresh keeps this child off the network
# and off the database, so the only behaviour under test is the report for a cycle it cannot emit.
def _no_refresh(*_args: Any, **_kwargs: Any) -> None:
    return None


def _no_sleep(_seconds: float) -> None:
    return None


run_collector_cycles(
    inputs,
    refresh=_no_refresh,
    sleep=_no_sleep,
    collector_id=COLLECTOR_ID,
    stop_after_cycles=2,
    environ={"DEEPSEEK_API_KEY": "test-key-123"},
)
print("cycles done")
