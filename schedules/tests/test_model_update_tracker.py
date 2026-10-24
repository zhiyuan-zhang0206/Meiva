# meiva:note
# The upstream model tracker.
#
# What the P0 lead is told is decided from the checker's exit status and report. The shell runs
# a real child process standing in for the checker (`children/fake_scan.py`, which can leave a
# `last-report.json` behind) and talks to a gateway on a loopback port.

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.agents.context import MeivaContext
from meiva.gateway_client import use_client
from schedules.jobs.model_tracker import ModelTracker, run_tracker
from schedules.plans.directory_plan import ReportRecipient
from schedules.plans.tracker_report import (
    Failed,
    Quiet,
    ReadCandidates,
    candidate_message,
    plan_tracker_exit,
)
from schedules.tests.gateway_directory import (
    AgentRowSpec,
    DirectoryPage,
    GatewayRequest,
    serve_gateway,
)

_SLOT = datetime(2026, 9, 7, 6, 0, tzinfo=UTC)
_FAKE_CHECKER = Path(__file__).parent / "children" / "fake_scan.py"
_LEAD = ReportRecipient("model-update-tracker", "MEIVA_MODEL_UPDATE_REPORT_AGENT", "lead", None)
_PAGES = (DirectoryPage("lead", None, (AgentRowSpec(17, "lead", "idling"),), None),)
_REPORT = {
    "actionable_candidates": {"openai": ["gpt-9", "gpt-9-mini"]},
    "providers": {
        "openai": {"series_models": {"gpt-9": ["gpt-8", "gpt-8-fast"], "gpt-9-mini": []}}
    },
    "status_changes": ["anthropic", "google"],
}


def test_exit_zero_is_quiet_and_exit_two_reads_the_candidates() -> None:
    assert plan_tracker_exit(0, "") == Quiet()
    assert plan_tracker_exit(2, "") == ReadCandidates()


def test_any_other_exit_is_a_failure_with_the_output_tail() -> None:
    verdict = plan_tracker_exit(1, "x" * 1500 + "boom")

    assert verdict == Failed(
        "Model update tracker failed:\nexit code 1\n"
        + "x" * 984
        + "boom\nCheck the schedule log and report directory."
    )


def test_the_candidates_message_lists_each_model_and_what_the_registry_already_binds() -> None:
    assert candidate_message(_REPORT, "/reports/last-report.md") == (
        "New upstream model candidates:\n"
        "- openai: gpt-9 (registry already binds gpt-8, gpt-8-fast in this series; "
        "review supersession)\n"
        "- openai: gpt-9-mini\n"
        "Provider status changed: anthropic, google\n"
        "Full report: /reports/last-report.md"
    )


@pytest.mark.parametrize(
    "report",
    [
        [],
        {"actionable_candidates": [], "providers": {}, "status_changes": []},
        {"actionable_candidates": {"p": "gpt"}, "providers": {"p": {}}, "status_changes": []},
        {
            "actionable_candidates": {"p": [1]},
            "providers": {"p": {"series_models": {}}},
            "status_changes": [],
        },
    ],
)
def test_a_malformed_report_raises_instead_of_being_summarised(report: object) -> None:
    with pytest.raises(TypeError):
        candidate_message(report, "r.md")


def test_a_malformed_status_list_is_refused() -> None:
    report = {**_REPORT, "status_changes": [1]}
    with pytest.raises(ValueError, match="status_changes must be a list of strings"):
        candidate_message(report, "r.md")


def _tracker(tmp_path: Path, *args: str, timeout: float = 30.0) -> ModelTracker:
    report_dir = tmp_path / "model-tracker"
    return ModelTracker(
        command=(sys.executable, str(_FAKE_CHECKER), "--write-report", str(report_dir), *args),
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path)},
        report_dir=report_dir,
        timeout=timeout,
        recipient=_LEAD,
    )


def _sent(requests: list[GatewayRequest]) -> list[str]:
    return [str(r.body["content"]) for r in requests if r.path.endswith("/messages")]


def test_a_quiet_check_tells_nobody(
    sdk_environment: MeivaContext, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with serve_gateway(_PAGES) as gateway, gateway.client() as client, use_client(client):
        run_tracker(_SLOT, None, tracker=_tracker(tmp_path))

    assert gateway.requests == []
    assert "model update tracker: no new candidates" in capsys.readouterr().out


def test_new_candidates_reach_the_p0_lead_with_the_report_path(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    tracker = _tracker(tmp_path, "--exit", "2", "--report-json", json.dumps(_REPORT))

    with serve_gateway(_PAGES) as gateway, gateway.client() as client, use_client(client):
        run_tracker(_SLOT, None, tracker=tracker)

    assert _sent(gateway.requests) == [
        candidate_message(_REPORT, str(tmp_path / "model-tracker" / "last-report.md"))
    ]


def test_a_failing_checker_is_reported_with_its_output(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    tracker = _tracker(tmp_path, "--exit", "1", "--stderr", "network down")

    with serve_gateway(_PAGES) as gateway, gateway.client() as client, use_client(client):
        run_tracker(_SLOT, None, tracker=tracker)

    [message] = _sent(gateway.requests)
    assert message.startswith("Model update tracker failed:\nexit code 1\nnetwork down")


def test_a_checker_that_outlives_its_bound_is_reported_as_a_failure(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    tracker = _tracker(tmp_path, "--sleep", "30", timeout=0.5)

    with serve_gateway(_PAGES) as gateway, gateway.client() as client, use_client(client):
        run_tracker(_SLOT, None, tracker=tracker)

    [message] = _sent(gateway.requests)
    assert message.startswith("Model update tracker failed:\nsubprocess exception:")
    assert "timed out" in message


def test_a_report_the_checker_wrote_malformed_is_reported_as_a_failure(
    sdk_environment: MeivaContext, tmp_path: Path
) -> None:
    tracker = _tracker(tmp_path, "--exit", "2", "--report-json", "[]")

    with serve_gateway(_PAGES) as gateway, gateway.client() as client, use_client(client):
        run_tracker(_SLOT, None, tracker=tracker)

    [message] = _sent(gateway.requests)
    assert "report must be an object" in message
