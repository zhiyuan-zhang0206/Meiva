# meiva:note
# The daily CI-minute reconciliation.
#
# What it reports is decided from values (the slot's window and day, the summary of the ledger
# rows). The reading half runs against `gh` answers of the real shape through the host's command
# runner and a real ledger file; the failure path wakes the P0 lead through a gateway on a
# loopback port and leaves the slot claimed.

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import psycopg
import pytest

from base.agents.context import MeivaContext
from base.cluster.machine import MachineIdentityReader
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.gateway_client import use_client
from schedules.jobs.c9_report import C9Report, reconcile_window, report_day
from schedules.jobs.catchup import Fired, fire_slot_once
from schedules.plans.c9_window import (
    CiRun,
    MinuteRates,
    failure_message,
    summarize,
    window_bounds,
)
from schedules.plans.directory_plan import ReportRecipient
from schedules.tests.gateway_directory import AgentRowSpec, DirectoryPage, serve_gateway
from schedules.tests.schedule_support import claimed_slots, insert_schedule

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SLOT = datetime(2026, 9, 6, 21, 0, tzinfo=UTC)
_RATES = MinuteRates(linux_usd=0.006, macos_usd=0.062)
_LEAD = ReportRecipient("c9-daily-report", "MEIVA_CI_USAGE_REPORT_AGENT", "lead", None)


def test_manifest_declares_c9_daily_report_schedule() -> None:
    manifest = json.loads((_REPO_ROOT / "schedules" / "manifest.json").read_text())
    entry = next(
        item for item in manifest["builtin_schedules"] if item["name"] == "c9-daily-report"
    )

    assert (_REPO_ROOT / "schedules" / entry["script"]).is_file()
    assert entry["script"] == "c9-daily-report-schedule.py"
    assert entry["command"] == "python c9-daily-report-schedule.py"
    assert entry["class"] == "product"
    assert entry["default_enabled"] is True
    assert "05:00 cluster time" in entry["description"]


# meiva:note
# The day label must follow the cluster wall clock, not the test environment's zone (CI runs
# UTC): 05:00 Asia/Shanghai on 2026-09-07 is 21:00 UTC on 2026-09-06, so the UTC date and the
# cluster day differ.
def test_window_bounds_covers_the_slot_day_in_cluster_timezone() -> None:
    since, until, day = window_bounds(_SLOT, "Asia/Shanghai")

    assert since == "2026-09-05T21:00:00Z"
    assert until == "2026-09-06T21:00:00Z"
    assert day == "2026-09-07"


def test_a_naive_slot_has_no_window() -> None:
    with pytest.raises(ValueError, match="slot_end must carry tzinfo"):
        window_bounds(_SLOT.replace(tzinfo=None), "UTC")


def test_summarize_splits_attribution_and_meters_minutes() -> None:
    runs = [CiRun(5811, 10, 20), CiRun(405, 5, 0), CiRun(None, 7, 3)]

    payload = summarize(
        runs,
        day="2026-09-07",  # time-bomb-ok: explicit fixture window input
        window_start="2026-09-05T21:00:00Z",  # time-bomb-ok: explicit fixture window input
        window_end="2026-09-06T21:00:00Z",  # time-bomb-ok: explicit fixture window input
        appended_runs=2,
        rates=_RATES,
    )

    assert payload == {
        "day": "2026-09-07",  # time-bomb-ok: explicit fixture window input
        "window_start": "2026-09-05T21:00:00Z",  # time-bomb-ok: explicit fixture window input
        "window_end": "2026-09-06T21:00:00Z",  # time-bomb-ok: explicit fixture window input
        "runs": 3,
        "attributed_runs": 2,
        "unattributed_runs": 1,
        "total_minutes": 45,
        "attributed_minutes": 35,
        "linux_minutes": 22,
        "macos_minutes": 23,
        "appended_runs": 2,
        "est_usd": round(22 * 0.006 + 23 * 0.062, 2),
    }


def test_the_failure_message_names_the_manual_backfill() -> None:
    assert failure_message("collector failed") == (
        "C9 daily reconciliation failed:\ncollector failed\n"
        "Check the schedule log; backfill the missed window manually with "
        "`scripts/ci/pull_requests/accounting.py --since ... --until ... --append-ledger`."
    )


_CI_RUN: dict[str, object] = {
    "id": 7001,
    "name": "CI",
    "created_at": "2026-09-06T10:00:00Z",
    "status": "completed",
    "conclusion": "success",
    "head_branch": "feature",
    "head_sha": "abc123",
    "pull_requests": [],
    "head_commit": {"message": "Count the minutes (task #42)"},
}
_JOBS = {
    "total_count": 2,
    "jobs": [
        {
            "id": 1,
            "name": "linux unit",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-09-06T10:00:00Z",
            "completed_at": "2026-09-06T10:10:00Z",
        },
        {
            "id": 2,
            "name": "macOS app",
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-09-06T10:00:00Z",
            "completed_at": "2026-09-06T10:20:00Z",
        },
    ],
}


def _github(commands: FakeCommands) -> None:
    commands.on(
        lambda argv: argv[:2] == ("gh", "api") and "/actions/runs?" in argv[2],
        Reply(stdout=json.dumps({"total_count": 1, "workflow_runs": [_CI_RUN]})),
        repeat=True,
    )
    commands.on(
        lambda argv: argv[:2] == ("gh", "api") and "/actions/runs/7001/jobs" in argv[2],
        Reply(stdout=json.dumps(_JOBS)),
        repeat=True,
    )


def _job(tmp_path: Path, commands: FakeCommands, recipient: ReportRecipient = _LEAD) -> C9Report:
    return C9Report(
        tz="Asia/Shanghai",
        recipient=recipient,
        repo="owner/repo",
        ledger=tmp_path / "ledger.jsonl",
        host=fake_host(commands),
    )


def test_the_slots_window_is_reconciled_into_the_ledger(tmp_path: Path) -> None:
    commands = FakeCommands()
    _github(commands)
    job = _job(tmp_path, commands)

    first = reconcile_window(_SLOT, job)
    again = reconcile_window(_SLOT, job)

    assert first.day == "2026-09-07"
    assert (first.runs, first.appended) == (1, 1)
    assert first.attributes["window_start"] == "2026-09-05T21:00:00Z"
    assert first.attributes["window_end"] == "2026-09-06T21:00:00Z"
    assert first.attributes["linux_minutes"] == 10
    assert first.attributes["macos_minutes"] == 20
    assert first.attributes["est_usd"] == 1.3
    assert (again.runs, again.appended) == (1, 0)
    [row] = (tmp_path / "ledger.jsonl").read_text().splitlines()
    assert json.loads(row)["run_id"] == 7001
    assert json.loads(row)["task_id"] == 42


def test_a_failed_reconciliation_wakes_the_p0_lead_and_the_claim_stays(
    db_conn: psycopg.Connection,
    database: Database,
    machine_identity: MachineIdentityReader,
    emitted_events: EmittedEvents,
    sdk_environment: MeivaContext,
    tmp_path: Path,
) -> None:
    commands = FakeCommands()
    commands.on(("gh", "api"), Reply(stderr="gh api down", returncode=1), repeat=True)
    job = _job(tmp_path, commands)
    inputs = ScheduleInputs(
        lambda: database,
        emitted_events.producer,
        LoadedCommit(_REPO_ROOT, None),
        machine=machine_identity,
    )
    schedule_id = insert_schedule(db_conn, name="c9-failure", created_at=_SLOT)
    pages = (DirectoryPage("lead", None, (AgentRowSpec(17, "lead", "idling"),), None),)

    with serve_gateway(pages) as gateway, gateway.client() as client, use_client(client):
        fired = fire_slot_once(
            database,
            _SLOT,
            None,
            schedule_id=schedule_id,
            fire=partial(report_day, job=job, inputs=inputs),
        )
        retried = fire_slot_once(
            database,
            _SLOT,
            None,
            schedule_id=schedule_id,
            fire=partial(report_day, job=job, inputs=inputs),
        )

    assert fired == Fired(_SLOT, None)
    assert retried is None
    assert claimed_slots(db_conn, schedule_id) == [_SLOT]
    [message] = [request for request in gateway.requests if request.path.endswith("/messages")]
    assert message.path == "/api/agents/17/messages"
    content = str(message.body["content"])
    assert content.startswith("C9 daily reconciliation failed:\nCiUsageReadError:")
    assert "GitHub query failed (exit 1)" in content
    assert "backfill the missed window manually" in content
    assert len(emitted_events) == 0


def test_the_failure_is_attributed_to_the_commands_that_ran(tmp_path: Path) -> None:
    commands = FakeCommands()
    commands.on(("gh", "api"), Reply(stderr="gh api down", returncode=1), repeat=True)

    with pytest.raises(Exception, match="GitHub query failed"):
        reconcile_window(_SLOT, _job(tmp_path, commands))

    assert all(argv[:2] == ("gh", "api") for argv in commands.argvs)
    assert not (tmp_path / "ledger.jsonl").exists()


def test_a_command_that_cannot_run_is_the_same_failure(tmp_path: Path) -> None:
    commands = FakeCommands()
    commands.on(("gh", "api"), subprocess.TimeoutExpired("gh", 60), repeat=True)

    with pytest.raises(Exception, match="GitHub query timed out"):
        reconcile_window(_SLOT, _job(tmp_path, commands))
