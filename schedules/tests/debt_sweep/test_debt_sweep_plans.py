# meiva:note
# What the daily debt sweep decides, asserted on the values it returns.
#
# How a scan outcome becomes a report and a summary, which day and file a slot's report belongs
# to, what the clearing worker is told, what the telemetry event carries, what the P0 lead hears
# on a failure and what the dry run prints. Nothing here runs the scan or calls the gateway; the
# shell that acts on these answers is tested in `test_debt_sweep_shell.py`.

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.events.contract import payload_keys
from schedules.plans.debt_sweep_scan import (
    ScanExited,
    ScanReport,
    ScanUnrunnable,
    scan_artifact_path,
    scan_report,
    slot_day,
)
from schedules.plans.debt_sweep_worker import (
    DEFAULT_WORKER_LABEL,
    LEDGER_PATH,
    dry_run_text,
    event_payload,
    failure_message,
    log_line,
    parse_worker_label,
    worker_prompt,
)

_SLOT = datetime(2026, 9, 21, 22, 30, tzinfo=UTC)
_ARTIFACT = Path("/home/debt-sweeps/scan.txt")
_OK = ScanReport(succeeded=True, summary="mechanical scan completed (3 lines)", text="a\nb\nc\n")
_FAILED = ScanReport(succeeded=False, summary="mechanical scan failed: exit 2", text="boom\n")


def test_a_scan_that_exits_zero_keeps_its_output_as_the_report() -> None:
    report = scan_report(ScanExited(0, "candidate evidence\nsecond line\n"))

    assert report == ScanReport(
        succeeded=True,
        summary="mechanical scan completed (2 lines; final: second line)",
        text="candidate evidence\nsecond line\n",
    )


def test_a_scan_that_exits_non_zero_is_a_failed_report_that_names_the_exit() -> None:
    report = scan_report(ScanExited(3, "partial output\n"))

    assert report.succeeded is False
    assert report.text == "partial output\n\nmechanical scan exited 3\n"
    assert report.summary == "mechanical scan failed (2 lines; final: mechanical scan exited 3)"


def test_a_scan_that_could_not_run_is_a_failed_report_with_the_error() -> None:
    report = scan_report(ScanUnrunnable("TimeoutExpired: timed out after 900 seconds"))

    assert report.succeeded is False
    assert (
        report.text
        == "mechanical scan could not run: TimeoutExpired: timed out after 900 seconds\n"
    )
    assert report.summary.startswith(
        "mechanical scan failed (1 lines; final: mechanical scan could"
    )


def test_a_silent_scan_says_so_and_a_long_final_line_is_cut() -> None:
    assert (
        scan_report(ScanExited(0, "")).summary
        == "mechanical scan completed (0 lines; final: no output)"
    )
    assert scan_report(ScanExited(0, "\n  \n")).summary == (
        "mechanical scan completed (0 lines; final: no output)"
    )
    long = scan_report(ScanExited(0, "x" * 500 + "\n"))
    assert long.summary == f"mechanical scan completed (1 lines; final: {'x' * 240})"


def test_the_day_is_the_slots_date_in_the_cluster_timezone() -> None:
    assert slot_day(_SLOT, "Asia/Shanghai") == "2026-09-22"
    assert slot_day(_SLOT, "UTC") == "2026-09-21"
    assert slot_day(_SLOT, "America/Los_Angeles") == "2026-09-21"


def test_a_slot_without_a_zone_has_no_day_and_no_file() -> None:
    naive = _SLOT.replace(tzinfo=None)

    with pytest.raises(ValueError, match="slot must carry tzinfo"):
        slot_day(naive, "UTC")
    with pytest.raises(ValueError, match="slot must carry tzinfo"):
        scan_artifact_path(Path("/home"), "2026-09-22", naive)


def test_the_report_is_stored_under_its_day_and_the_slots_utc_stamp() -> None:
    assert scan_artifact_path(Path("/home"), "2026-09-22", _SLOT) == Path(
        "/home/debt-sweeps/2026-09-22/mechanical-scan-20260921T223000Z.txt"
    )


def test_the_worker_prompt_names_the_ledger_the_scan_artifact_and_one_pr() -> None:
    prompt = worker_prompt("2026-09-22", _ARTIFACT, _FAILED)

    assert "today's debt-clearing pass" in prompt
    assert "(2026-09-22)" in prompt
    assert "origin/main" in prompt
    assert "meiva.skills.sweeper" in prompt
    assert LEDGER_PATH == "future/tech-debt/ledger.md"
    assert f"`{LEDGER_PATH}`" in prompt
    assert f"`{_ARTIFACT}`" in prompt
    assert "mechanical scan failed: exit 2" in prompt
    assert "docs/conventions/engineering/tech-debt.md" in prompt
    assert "chore(sweeper): debt reconcile 2026-09-22" in prompt
    assert "end your own process" in prompt
    assert "meiva-sweeper" not in prompt


def test_the_event_payload_is_exactly_the_declared_event() -> None:
    payload = event_payload("2026-09-22", _OK, "messaged", 77)

    assert payload == {
        "day": "2026-09-22",  # time-bomb-ok: passthrough payload assertion, no clock-derived window
        "scan_status": "ok",
        "action": "messaged",
        "worker_agent_id": 77,
    }
    assert tuple(payload) == payload_keys("debt_sweep_daily")


def test_a_failed_scan_is_reported_as_failed_in_the_event() -> None:
    assert event_payload("2026-09-22", _FAILED, "spawned", 44)["scan_status"] == "failed"


def test_the_p0_lead_hears_the_end_of_a_long_failure() -> None:
    message = failure_message("head-" + "x" * 2000 + "-tail")

    assert message.startswith("Daily debt sweep failed:\n")
    assert "-tail\n" in message
    assert "head-" not in message
    assert message.endswith("then run the debt-clearing pass manually with the sweeper skills.")
    assert "saved mechanical-scan report" in message


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        (None, DEFAULT_WORKER_LABEL),
        ("", DEFAULT_WORKER_LABEL),
        ("   ", DEFAULT_WORKER_LABEL),
        ("custom-sweeper", "custom-sweeper"),
        ("  padded  ", "padded"),
    ],
)
def test_the_worker_label_defaults_unless_the_operator_names_one(
    raw: str | None, label: str
) -> None:
    assert parse_worker_label(raw) == label
    assert DEFAULT_WORKER_LABEL == "debt-sweep-daily"


def test_the_run_is_logged_with_its_day_scan_status_and_worker() -> None:
    assert log_line(_SLOT, "2026-09-22", _OK, "resurrected", 12) == (
        "[2026-09-21T22:30:00+00:00] debt-sweep-daily: 2026-09-22 \u2014 "
        "scan=ok, resurrected worker 12"
    )
    assert "scan=failed, spawned worker 44" in log_line(_SLOT, "2026-09-22", _FAILED, "spawned", 44)


def test_the_dry_run_prints_what_a_real_run_would_send_and_nothing_else() -> None:
    text = dry_run_text("2026-09-22", _ARTIFACT, _OK, "debt-sweep-daily")

    lines = text.splitlines()
    assert lines[0] == f"dry-run: mechanical scan report would be written to: {_ARTIFACT}"
    assert lines[1] == "dry-run: mechanical scan summary: mechanical scan completed (3 lines)"
    assert lines[2:6] == ["dry-run: mechanical scan report:", "a", "b", "c"]
    assert lines[6] == "dry-run: resolved worker label: debt-sweep-daily"
    assert lines[7] == "dry-run: prompt preview:"
    assert text.index(worker_prompt("2026-09-22", _ARTIFACT, _OK)) > text.index(lines[7])
    payload_line = lines[lines.index("dry-run: event payload:") + 1]
    assert json.loads(payload_line) == event_payload("2026-09-22", _OK, "spawned", 0)
    assert lines[-1] == (
        "dry-run: no claims, agent operations, telemetry, or database access were performed."
    )
    assert text.endswith("\n")


def test_a_report_without_a_final_newline_still_ends_its_line() -> None:
    bare = ScanReport(succeeded=True, summary="s", text="no newline")

    assert "dry-run: mechanical scan report:\nno newline\ndry-run: resolved" in dry_run_text(
        "2026-09-22", _ARTIFACT, bare, "x"
    )
    empty = ScanReport(succeeded=True, summary="s", text="")
    assert "dry-run: mechanical scan report:\n\ndry-run: resolved" in dry_run_text(
        "2026-09-22", _ARTIFACT, empty, "x"
    )
