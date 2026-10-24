# meiva:note
# The daily debt sweep against real resources.
#
# The mechanical scan is a real bash program: each test builds a small repository under
# `tmp_path` whose `scripts/audit/tech_debt_candidates.sh` prints, fails or sleeps as the test
# needs. The clearing worker and the P0 lead are reached through a gateway on a loopback port
# that serves the directory pages a test scripts and records every request; the claim is the
# throwaway Postgres; the manual `--once --dry-run` run is the script itself in a child
# interpreter.

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal

import psycopg
import pytest

from base.agents.context import MeivaContext
from base.cluster.machine import MachineIdentity
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.host.proc import run_bounded
from base.native_process.loaded_commit import LoadedCommit
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.gateway_client import use_client
from schedules.jobs.agent_directory import AgentDispatch
from schedules.jobs.catchup import Fired, fire_slot_once
from schedules.jobs.debt_sweep import (
    DebtSweep,
    SweepDone,
    parse_cli,
    run_scan,
    run_sweep,
    sweep_day,
)
from schedules.plans.debt_sweep_scan import ScanExited, ScanUnrunnable
from schedules.plans.directory_plan import ReportRecipient
from schedules.tests.gateway_directory import AgentRowSpec, DirectoryPage, serve_gateway
from schedules.tests.schedule_support import claimed_slots, insert_schedule

_SCHEDULES = Path(__file__).resolve().parents[2]
_SCRIPT = _SCHEDULES / "debt-sweep-daily-schedule.py"
_SLOT = datetime(2026, 9, 21, 22, 30, tzinfo=UTC)
_LABEL = "debt-sweep-daily"
_LEAD = ReportRecipient("debt-sweep-daily", "MEIVA_DEBT_SWEEP_REPORT_AGENT", "lead", 17)


def _repo(tmp_path: Path, script_body: str) -> Path:
    repo = tmp_path / "repo"
    program = repo / "scripts" / "audit" / "tech_debt_candidates.sh"
    program.parent.mkdir(parents=True)
    program.write_text(script_body, encoding="utf-8")
    return repo


_EVIDENCE = "echo \"args: $*\"\necho 'candidate evidence'\necho 'a warning' >&2\n"


def _job(tmp_path: Path, repo: Path, *, timeout: float = 60.0) -> DebtSweep:
    return DebtSweep(
        repo=repo,
        home=tmp_path / "home",
        tz="Asia/Shanghai",
        worker_label=_LABEL,
        scan_timeout=timeout,
    )


def test_the_scan_runs_the_repositorys_candidate_tool_and_keeps_both_streams(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path, _EVIDENCE)

    outcome = run_scan(repo, 60.0)

    assert outcome == ScanExited(0, f"args: --repo {repo}\ncandidate evidence\na warning\n")


def test_a_scan_that_exits_non_zero_is_reported_with_its_status(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "echo 'half done'\nexit 3\n")

    assert run_scan(repo, 60.0) == ScanExited(3, "half done\n")


def test_a_repository_without_the_candidate_tool_fails_inside_bash(tmp_path: Path) -> None:
    outcome = run_scan(tmp_path, 60.0)

    assert isinstance(outcome, ScanExited)
    assert outcome.returncode == 127
    assert "No such file or directory" in outcome.output


def test_a_scan_that_outlives_its_bound_could_not_run(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "sleep 30\n")

    outcome = run_scan(repo, 0.3)

    assert isinstance(outcome, ScanUnrunnable)
    assert outcome.error.startswith("TimeoutExpired: ")


@pytest.mark.parametrize(
    ("page", "action", "expected_id", "route"),
    [
        (
            DirectoryPage(_LABEL, None, (AgentRowSpec(12, _LABEL, "terminated"),), None),
            "resurrected",
            12,
            "/api/agents/12/resurrect",
        ),
        (
            DirectoryPage(_LABEL, None, (AgentRowSpec(13, _LABEL, "idling"),), None),
            "messaged",
            13,
            "/api/agents/13/messages",
        ),
        (DirectoryPage(_LABEL, None, (), None), "spawned", 400, "/api/keyed/v1/agents"),
    ],
)
def test_a_sweep_keeps_the_report_and_gives_the_worker_the_report_as_evidence(
    sdk_environment: MeivaContext,
    tmp_path: Path,
    page: DirectoryPage,
    action: Literal["spawned", "resurrected", "messaged"],
    expected_id: int,
    route: str,
) -> None:
    repo = _repo(tmp_path, _EVIDENCE)
    job = _job(tmp_path, repo)

    with serve_gateway((page,)) as gateway, gateway.client() as client, use_client(client):
        done = run_sweep(_SLOT, job)

    artifact = (
        tmp_path / "home" / "debt-sweeps" / "2026-09-22" / "mechanical-scan-20260921T223000Z.txt"
    )
    assert done == SweepDone("2026-09-22", artifact, done.scan, AgentDispatch(expected_id, action))
    assert (
        artifact.read_text(encoding="utf-8")
        == f"args: --repo {repo}\ncandidate evidence\na warning\n"
    )
    assert done.scan.succeeded is True
    [dispatch] = [request for request in gateway.requests if request.method == "POST"]
    assert dispatch.path == route
    prompt = str(dispatch.body.get("prompt") or dispatch.body.get("content"))
    assert f"`{artifact}`" in prompt
    assert done.scan.summary in prompt
    assert "(2026-09-22)" in prompt


def test_a_failed_scan_still_wakes_the_worker_with_the_failure_in_the_prompt(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    repo = _repo(tmp_path, "echo 'broken'\nexit 2\n")
    job = _job(tmp_path, repo)
    page = DirectoryPage(_LABEL, None, (), None)

    with serve_gateway((page,)) as gateway, gateway.client() as client, use_client(client):
        done = run_sweep(_SLOT, job)

    assert done.scan.succeeded is False
    assert done.artifact.read_text(encoding="utf-8") == "broken\n\nmechanical scan exited 2\n"
    [spawn] = [request for request in gateway.requests if request.path == "/api/keyed/v1/agents"]
    assert "mechanical scan failed" in str(spawn.body["prompt"])
    assert spawn.body["label"] == _LABEL


def test_a_failed_sweep_wakes_the_p0_lead_and_the_claim_stays(
    sdk_environment: MeivaContext,
    db_conn: psycopg.Connection,
    database: Database,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _repo(tmp_path, _EVIDENCE)
    job = _job(tmp_path, repo)
    inputs = ScheduleInputs(
        lambda: database,
        emitted_events.producer,
        LoadedCommit(repo, None),
        machine=machine_identity,
    )
    schedule_id = insert_schedule(db_conn, name="debt-sweep-failure", created_at=_SLOT)
    page = DirectoryPage(_LABEL, None, (), None)
    fire = partial(sweep_day, job=job, recipient=_LEAD, inputs=inputs)

    with (
        serve_gateway((page,), refuse=frozenset({"/api/keyed/v1/agents"})) as gateway,
        gateway.client() as client,
        use_client(client),
    ):
        fired = fire_slot_once(database, _SLOT, None, schedule_id=schedule_id, fire=fire)
        retried = fire_slot_once(database, _SLOT, None, schedule_id=schedule_id, fire=fire)

    assert fired == Fired(_SLOT, None)
    assert retried is None
    assert claimed_slots(db_conn, schedule_id) == [_SLOT]
    [message] = [request for request in gateway.requests if request.path.endswith("/messages")]
    assert message.path == "/api/agents/17/messages"
    content = str(message.body["content"])
    assert content.startswith("Daily debt sweep failed:\n")
    assert "saved mechanical-scan report" in content
    assert "debt-sweep-daily failed: " in capsys.readouterr().out
    assert len(emitted_events) == 0
    assert (tmp_path / "home" / "debt-sweeps" / "2026-09-22").is_dir()


def test_a_p0_lead_that_cannot_be_reached_is_an_error_not_a_silent_failure(
    sdk_environment: MeivaContext,
    database: Database,
    machine_identity: MachineIdentity,
    emitted_events: EmittedEvents,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    job = _job(tmp_path, _repo(tmp_path, _EVIDENCE))
    inputs = ScheduleInputs(
        lambda: database,
        emitted_events.producer,
        LoadedCommit(tmp_path, None),
        machine=machine_identity,
    )
    unreachable = ReportRecipient("debt-sweep-daily", "MEIVA_DEBT_SWEEP_REPORT_AGENT", "lead", None)
    page = DirectoryPage(_LABEL, None, (), None)

    with (
        serve_gateway((page,), refuse=frozenset({"/api/keyed/v1/agents"})) as gateway,
        gateway.client() as client,
        use_client(client),
        pytest.raises(Exception, match="404"),
    ):
        sweep_day(_SLOT, None, job=job, recipient=unreachable, inputs=inputs)

    out = capsys.readouterr().out
    assert "debt-sweep-daily failed: " in out
    assert "debt-sweep-daily could not notify the P0 lead" in out
    assert not [request for request in gateway.requests if request.path.endswith("/messages")]


@pytest.mark.parametrize(
    "argv",
    [["--dry-run"], ["--once"], ["--repo", "somewhere"], ["--unknown-flag"], ["stray"]],
)
def test_only_the_manual_dry_run_flags_are_accepted(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        parse_cli(argv)

    assert refused.value.code == 2


def test_the_runner_starts_the_script_with_no_flags_and_a_dry_run_names_its_repository() -> None:
    assert parse_cli([]).dry_run is False
    cli = parse_cli(["--once", "--dry-run", "--repo", "/repo"])
    assert cli.dry_run is True
    assert cli.repo == Path("/repo")


def _run_script(
    tmp_path: Path, *args: str, label: str | None = None
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "home").mkdir(exist_ok=True)
    env = {
        **os.environ,
        "MEIVA_HOME": str(tmp_path / "home"),
        "MEIVA_TIMEZONE": "Asia/Shanghai",
        "MEIVA_CONFIG_FETCH": "skip",
    }
    env.pop("MEIVA_DEBT_SWEEP_AGENT_LABEL", None)
    if label is not None:
        env["MEIVA_DEBT_SWEEP_AGENT_LABEL"] = label
    return run_bounded(
        [sys.executable, "-I", str(_SCRIPT), *args],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_dry_run_scans_and_prints_without_leaving_anything_behind(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _EVIDENCE)

    child = _run_script(
        tmp_path, "--once", "--dry-run", "--repo", str(repo), label="custom-sweeper"
    )

    assert child.returncode == 0, child.stderr
    lines = child.stdout.splitlines()
    assert lines[0].startswith("dry-run: mechanical scan report would be written to: ")
    assert f"args: --repo {repo.resolve()}" in lines
    assert "candidate evidence" in lines
    assert (
        "dry-run: mechanical scan summary: mechanical scan completed (3 lines; final: a warning)"
        in lines
    )
    assert "dry-run: resolved worker label: custom-sweeper" in lines
    assert "dry-run: prompt preview:" in lines
    assert '{"action": "spawned", "day": ' in child.stdout
    assert lines[-1] == (
        "dry-run: no claims, agent operations, telemetry, or database access were performed."
    )
    assert not (tmp_path / "home" / "debt-sweeps").exists()


def test_a_dry_run_flag_without_its_pair_is_refused_by_the_script(tmp_path: Path) -> None:
    child = _run_script(tmp_path, "--dry-run")

    assert child.returncode == 2
    assert "--dry-run requires --once" in child.stderr
