# meiva:note
# The schedule entry's own behaviour, kept out of `test_runner.py`: what the entry does with the
# pipeline it is handed, and what the loop does when it cannot be handed one. The refresh cycle
# itself is `test_runner.py`'s subject; these tests never let a cycle touch the database or the
# network.
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, cast

from base.daemon.schedules.inputs import ScheduleInputs
from base.host.proc import run_bounded
from base.log import logger
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.tests.capture.event_capture import EmittedEvents
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig, UsageConfig
from meiva_builtins.plugins.meiva_usage.runner import run_collector_cycles


# meiva:note
# The loop's schedule inputs without a database: a no-op refresh never reads the handle, so the only
# thing these two tests exercise is what the loop does with a cycle event it cannot emit.
def _schedule_inputs_without_database(home: Path) -> ScheduleInputs:
    collector = CollectorConfig(
        id="scheduled-collector",
        label="Scheduled Collector",
        provider="deepseek",
        machine="company-air",
        key_env="DEEPSEEK_API_KEY",
        interval_minutes=1,
    )
    config_dir = home / "configs" / "meiva_usage"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_text(
        UsageConfig(collectors=(collector,)).model_dump_json(indent=2)
    )

    class _Machine:
        def name(self) -> str:
            return "company-air"

    return ScheduleInputs(
        database=cast(Any, lambda: None),
        producer=cast(Any, lambda: None),
        image=LoadedCommit(source_root=home, sha="test-sha"),
        machine=cast(Any, _Machine()),
        home=home,
    )


def _no_refresh(*_args: Any, **_kwargs: Any) -> None:
    return None


def _no_sleep(_seconds: float) -> None:
    return None


def _run_two_noop_cycles(inputs: ScheduleInputs) -> None:
    run_collector_cycles(
        inputs,
        refresh=_no_refresh,
        sleep=_no_sleep,
        collector_id="scheduled-collector",
        stop_after_cycles=2,
        environ={"DEEPSEEK_API_KEY": "test-key-123"},
    )


# meiva:note
# What the entry does with the pipeline it is handed: it binds it, and every cycle's event leaves
# the process (2026-11-06, task #6352). The control has to be a real process: this suite binds a
# pipeline of its own, which would hide a binder that never runs. The child runs the loop twice in
# one process, which is the second half of the claim — `init_telemetry` refuses a second live
# pipeline, so a binder without a per-process guard raises on the second run. The process name in
# those events is the role, not the schedule: a per-schedule name would flatten the metric surface
# across the fleet's schedules, so the schedule rides in the attributes instead.
def test_a_schedule_that_can_bind_emits_each_cycle_of_each_run() -> None:
    child = Path(__file__).with_name("child_emit_bound.py")

    result = run_bounded(
        [sys.executable, str(child)],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            **os.environ,
            "MEIVA_SCHEDULE_ID": "53",
            "PYTHONPATH": str(Path(__file__).resolve().parents[4]),
        },
    )

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["cycles"] == [
        {
            "collector": "scheduled-collector",
            "schedule_id": 53,
            "cycle": cycle,
            "process": "schedule-runner",
        }
        for _run in range(2)
        for cycle in (0, 1)
    ]
    assert summary["other_events"] == ["service_started"]


# meiva:note
# The fallback: an entry whose producer cannot hand over a pipeline must not stop the collector.
# The loop runs, and the drop is reported once per run, not once per cycle: a 15-minute loop would
# otherwise write 96 lines a day.
def test_a_cycle_event_that_cannot_be_emitted_says_so_once() -> None:
    child = Path(__file__).with_name("child_emit_unbound.py")

    result = run_bounded(
        [sys.executable, str(child)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[4])},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "cycles done drop_reports=1"
    assert "cannot bind its event pipeline" in result.stderr


# meiva:note
# The in-process control: this suite already bound a pipeline, so the binder must step aside and the
# loop must emit both cycles through it without a word about drops.
def test_a_bound_process_does_not_report_the_drop(
    tmp_path: Path, emitted_events: EmittedEvents
) -> None:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        _run_two_noop_cycles(_schedule_inputs_without_database(tmp_path))
    finally:
        logger.remove(sink)

    assert [a["cycle"] for a in emitted_events.attributes("plugin_collector_cycle")] == [0, 1]
    assert not [line for line in lines if "cannot be emitted" in line], lines
