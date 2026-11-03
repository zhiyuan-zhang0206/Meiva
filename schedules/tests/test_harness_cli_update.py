# meiva:note
# External harness CLI update plan and job runner tests.
# meiva:link schedules/README.md

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentityReader
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.host.env.harness.catalog import CATALOG
from base.host.env.harness.convergence import CommandResult, ConvergenceReport, HarnessUpdateOutcome
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from schedules.jobs.harness.harness_cli_update import execute_round
from schedules.plans.harness.update_plan import decide_round

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SLOT = datetime(2026, 11, 3, 4, 30, tzinfo=UTC)


def test_manifest_declares_harness_cli_update() -> None:
    manifest = json.loads((_REPO_ROOT / "schedules" / "manifest.json").read_text())
    entry = next(
        item for item in manifest["builtin_schedules"] if item["name"] == "harness-cli-update"
    )

    assert entry["class"] == "product"
    assert entry["default_enabled"] is True
    assert entry["placement"] == "per_machine"
    assert entry["script"] == "jobs/harness/harness-cli-update-schedule.py"
    assert entry["command"] == "python jobs/harness/harness-cli-update-schedule.py"
    assert (_REPO_ROOT / "schedules" / entry["script"]).is_file()


def test_decide_round_skipped_non_production() -> None:
    decision = decide_round(None, skipped_non_production=True)
    assert decision.telemetry_payload == {"status": "skipped", "reason": "non_production"}
    assert "skipped: non-production" in decision.log_line
    assert decision.failure_message is None


def test_decide_round_success() -> None:
    report = ConvergenceReport(
        outcomes=(
            HarnessUpdateOutcome("claude", "up_to_date", "0.2.14", "0.2.14", "0.2.14"),
            HarnessUpdateOutcome("codex", "updated", "0.1.0", "0.2.0", "0.2.0"),
        )
    )
    decision = decide_round(report)
    assert decision.telemetry_payload["status"] == "ok"
    assert decision.telemetry_payload["updated_count"] == 1
    assert decision.telemetry_payload["failed_count"] == 0
    assert decision.failure_message is None
    assert "1 updated, 0 failed, 1 up-to-date, 0 absent" in decision.log_line


def test_decide_round_failure() -> None:
    report = ConvergenceReport(
        outcomes=(
            HarnessUpdateOutcome(
                "claude", "failed", "0.2.0", "0.2.14", detail="npm install failed: EACCES"
            ),
        )
    )
    decision = decide_round(report)
    assert decision.telemetry_payload["status"] == "failed"
    assert decision.telemetry_payload["updated_count"] == 0
    assert decision.telemetry_payload["failed_count"] == 1
    assert decision.failure_message is not None
    assert "claude (npm install failed: EACCES)" in decision.failure_message


def test_execute_round_skips_on_non_production(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inputs = ScheduleInputs(
        lambda: database,
        emitted_events.producer,
        LoadedCommit(_REPO_ROOT, None),
        machine=machine_identity,
        home=tmp_path / "custom-home",
    )

    def _dummy_runner(
        command: str,
        *,
        timeout: float,
        env: Mapping[str, str],
    ) -> CommandResult:
        return CommandResult(exit_code=0, stdout="", stderr="")

    execute_round(
        _SLOT,
        None,
        inputs=inputs,
        user_home=tmp_path / "user",
        base_env={},
        is_production=False,
        run_shell=_dummy_runner,
    )

    out = capsys.readouterr().out
    assert "harness-cli-update: skipped: non-production identity" in out
    update_events = emitted_events.named("harness_cli_update_round")
    assert len(update_events) == 1
    assert update_events[0].attributes == {"status": "skipped", "reason": "non_production"}


def test_execute_round_runs_and_emits_telemetry_on_production(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / ".meiva"
    home.mkdir()
    user_home = tmp_path / "user"
    bin_dir = user_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    fake_claude = bin_dir / "claude"
    fake_claude.write_text("#!/bin/sh\n")
    fake_claude.chmod(0o755)

    inputs = ScheduleInputs(
        lambda: database,
        emitted_events.producer,
        LoadedCommit(_REPO_ROOT, None),
        machine=machine_identity,
        home=home,
    )

    def _runner(
        command: str,
        *,
        timeout: float,
        env: Mapping[str, str],
    ) -> CommandResult:
        for spec in CATALOG.values():
            if spec.cli in command:
                return CommandResult(exit_code=0, stdout=f"{spec.pinned_version}\n", stderr="")
        return CommandResult(exit_code=0, stdout="1.0.0\n", stderr="")

    execute_round(
        _SLOT,
        None,
        inputs=inputs,
        user_home=user_home,
        base_env={"PATH": "/usr/bin:/bin"},
        is_production=True,
        run_shell=_runner,
    )

    out = capsys.readouterr().out
    assert "harness-cli-update: convergence complete:" in out
    update_events = emitted_events.named("harness_cli_update_round")
    assert len(update_events) == 1
    assert update_events[0].attributes["status"] == "ok"


def test_execute_round_raises_on_failure(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    tmp_path: Path,
) -> None:
    home = tmp_path / ".meiva"
    home.mkdir()
    user_home = tmp_path / "user"
    bin_dir = user_home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    fake_claude = bin_dir / "claude"
    fake_claude.write_text("#!/bin/sh\n")
    fake_claude.chmod(0o755)

    inputs = ScheduleInputs(
        lambda: database,
        emitted_events.producer,
        LoadedCommit(_REPO_ROOT, None),
        machine=machine_identity,
        home=home,
    )

    def _failing_runner(
        command: str,
        *,
        timeout: float,
        env: Mapping[str, str],
    ) -> CommandResult:
        if "--version" in command:
            return CommandResult(exit_code=0, stdout="0.0.1\n", stderr="")
        return CommandResult(exit_code=1, stdout="", stderr="failed to install")

    with pytest.raises(RuntimeError, match="external harness CLI update failed"):
        execute_round(
            _SLOT,
            None,
            inputs=inputs,
            user_home=user_home,
            base_env={"PATH": "/usr/bin:/bin"},
            is_production=True,
            run_shell=_failing_runner,
        )

    update_events = emitted_events.named("harness_cli_update_round")
    assert len(update_events) == 1
    assert update_events[0].attributes["status"] == "failed"


def test_harness_cli_update_schedule_once_flag(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MEIVA_MACHINE_NAME=runner-test\n")
    script = _REPO_ROOT / "schedules" / "jobs" / "harness" / "harness-cli-update-schedule.py"
    done = subprocess.run(
        [sys.executable, str(script), "--once"],
        cwd=_REPO_ROOT,
        env={
            **os.environ,
            "MEIVA_HOME": str(tmp_path),
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_MACHINE_NAME": "runner-test",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "harness-cli-update: skipped: non-production identity" in done.stdout
