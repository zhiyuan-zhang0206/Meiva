# meiva:note
# Runner for the external harness CLI update schedule.
# meiva:link schedules/README.md

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from base.daemon.schedules.inputs import ScheduleInputs
from base.host.env.harness.convergence import (
    CommandResult,
    ShellRunner,
    converge_local_harness_clis,
)
from schedules.jobs.telemetry_event import emit_system_event
from schedules.plans.harness.update_plan import decide_round

_PROCESS_NAME = "harness-cli-update"
_EVENT_NAME = "harness_cli_update_round"


def default_shell_runner(command: str, *, timeout: float, env: Mapping[str, str]) -> CommandResult:
    try:
        res = subprocess.run(
            ["bash", "-c", command],
            timeout=timeout,
            capture_output=True,
            text=True,
            env=dict(env),
            check=False,
        )
        return CommandResult(
            exit_code=res.returncode,
            stdout=res.stdout or "",
            stderr=res.stderr or "",
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            exit_code=124,
            stdout=(exc.stdout or "") if isinstance(exc.stdout, str) else "",
            stderr=f"command timed out after {timeout}s",
        )


def execute_round(
    slot: datetime,
    _payload: str | None,
    *,
    inputs: ScheduleInputs,
    user_home: Path,
    base_env: Mapping[str, str],
    is_production: bool,
    run_shell: ShellRunner,
) -> None:
    if not is_production:
        decision = decide_round(None, skipped_non_production=True)
        print(f"[{slot.isoformat()}] {_PROCESS_NAME}: {decision.log_line}")
        emit_system_event(
            _EVENT_NAME,
            decision.telemetry_payload,
            process=_PROCESS_NAME,
            producer=inputs.producer,
            machine_reader=inputs.machine.name,
            image=inputs.image,
            home=inputs.home,
        )
        return

    report = converge_local_harness_clis(
        user_home=user_home,
        meiva_home=inputs.home,
        now=slot,
        source="schedule",
        base_env=base_env,
        run_shell=run_shell,
    )
    decision = decide_round(report)
    print(f"[{slot.isoformat()}] {_PROCESS_NAME}: {decision.log_line}")
    emit_system_event(
        _EVENT_NAME,
        decision.telemetry_payload,
        process=_PROCESS_NAME,
        producer=inputs.producer,
        machine_reader=inputs.machine.name,
        image=inputs.image,
        home=inputs.home,
    )
    if decision.failure_message:
        raise RuntimeError(decision.failure_message)
