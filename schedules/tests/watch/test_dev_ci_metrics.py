# meiva:note
# The daily Dev/CI metrics run.
#
# Which day is reported and what the P0 lead is told when sampling fails are decisions over values.
# The shell is run against a program standing in for `gh` (`fake_gh.py`, a real child process), the
# real CI exporter writing its cache and snapshot under `tmp_path`, and a gateway on a loopback
# port that records the messages sent to the P0 lead.

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from base.agents.context import MeivaContext
from base.clock import SYSTEM_TIMERS
from base.cluster.machine import MachineIdentityReader
from base.db import Database
from base.host.proc import run_bounded
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.gateway_client import use_client
from schedules.jobs.entry import cluster_timezone
from schedules.jobs.watch.ci_metrics import CiMetrics, collect_ci_metrics
from schedules.plans.ci_workflows import day_counts, failure_message, previous_day
from schedules.plans.directory_plan import ReportRecipient
from scripts.ci.pull_requests.gh_process import GhProcess
from schedules.tests.support.gateway_directory import (
    AgentRowSpec,
    DirectoryPage,
    GatewayRequest,
    serve_gateway,
)
from schedules.tests.support.schedule_support import borrowed_inputs

_ROOT = Path(__file__).resolve().parents[3]
_FAKE_GH = Path(__file__).resolve().parents[1] / "children" / "fake_gh.py"
_LEAD = ReportRecipient("dev-ci-metrics", "MEIVA_CI_METRICS_REPORT_AGENT", "lead", None)


def test_the_reported_day_is_the_cluster_day_before_the_slot() -> None:
    slot = datetime(2026, 9, 3, 22, 20, tzinfo=UTC)

    assert previous_day(slot, "Asia/Shanghai") == "2026-09-03"
    assert previous_day(slot, "UTC") == "2026-09-02"


def test_a_days_counts_come_from_the_snapshot() -> None:
    snapshot = {"repositories": {"o/r": {"days": {"2026-09-03": {"runs": 7, "failed_runs": 2}}}}}

    assert day_counts(snapshot, "2026-09-03", "o/r") == (7, 2)
    with pytest.raises(KeyError):
        day_counts(snapshot, "2026-09-04", "o/r")
    with pytest.raises(TypeError, match="expected an object"):
        day_counts([], "2026-09-03", "o/r")


def test_the_failure_message_names_the_backfill() -> None:
    assert failure_message("exporter failed") == (
        "Dev/CI metrics collection failed:\nexporter failed\n"
        "Check the schedule log; backfill with `scripts/ci/pull_requests/runs_export.py --repo "
        "zhiyuan-zhang0206/Meiva --print-snapshot`."
    )


def test_manifest_cron_is_the_collection_slot() -> None:
    manifest = json.loads((_ROOT / "schedules" / "manifest.json").read_text())
    entry = next(i for i in manifest["builtin_schedules"] if i["name"] == "dev-ci-metrics")
    assert entry["script"] == "dev-ci-metrics-schedule.py"
    assert "06:20 cluster time" in entry["description"]


def _gh(*, fail: bool = False):
    env = {**os.environ}
    if fail:
        env["FAKE_GH_FAIL"] = "1"

    def run(
        argv: Sequence[str],
        /,
        *,
        timeout: float,
        capture_output: Literal[True],
        text: Literal[True],
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(_FAKE_GH), *argv[1:]],
            env=env,
            timeout=timeout,
            capture_output=capture_output,
            text=text,
            check=False,
        )

    return run


def _job(state_dir: Path, run_process: GhProcess) -> CiMetrics:
    return CiMetrics(
        tz=cluster_timezone(),
        recipient=_LEAD,
        state_dir=state_dir,
        run_process=run_process,
        timers=SYSTEM_TIMERS,
    )


def _messages(requests: list[GatewayRequest]) -> list[str]:
    return [str(r.body["content"]) for r in requests if r.path.endswith("/messages")]


_PAGES = (DirectoryPage("lead", None, (AgentRowSpec(17, "lead", "idling"),), None),)


def test_a_quiet_collection_reports_the_previous_day_and_emits_the_snapshot(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    sdk_environment: MeivaContext,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_process = _gh()
    inputs = borrowed_inputs(database, emitted_events.producer, machine_identity, _ROOT)

    with serve_gateway(_PAGES) as gateway, gateway.client() as client, use_client(client):
        collect_ci_metrics(datetime.now(UTC), None, job=_job(tmp_path, run_process), inputs=inputs)

    assert _messages(gateway.requests) == []
    assert "runs, 0 failed" in capsys.readouterr().out
    assert (tmp_path / "snapshot.json").is_file()
    assert "ci_runs_daily" in emitted_events.names()


def test_a_failing_exporter_wakes_the_p0_lead_with_the_failure(
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    sdk_environment: MeivaContext,
    tmp_path: Path,
) -> None:
    inputs = borrowed_inputs(database, emitted_events.producer, machine_identity, _ROOT)

    with serve_gateway(_PAGES) as gateway, gateway.client() as client, use_client(client):
        collect_ci_metrics(
            datetime.now(UTC), None, job=_job(tmp_path, _gh(fail=True)), inputs=inputs
        )

    [message] = _messages(gateway.requests)
    assert message.startswith(
        "Dev/CI metrics collection failed:\nRuntimeError: ci-runs-export exited non-zero"
    )
    assert not (tmp_path / "snapshot.json").exists()


def test_ci_metrics_import_does_not_load_the_exporter(tmp_path: Path) -> None:
    user = tmp_path / "user"
    user.mkdir()
    home = tmp_path / "meiva"
    result = run_bounded(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "schedules.tests.children.ci_metrics_import_child",
        ],
        env={
            "HOME": str(user),
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
            "PATH": "/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert not home.exists()
