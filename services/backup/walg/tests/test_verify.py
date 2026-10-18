from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from base.config import settings
from services.backup.walg import verify
from services.backup.walg.gaps import (
    RecordedGap,
    RecordedGapError,
    load_recorded_gaps,
)
from services.backup.walg.runner import WalgCommandError
from services.backup.walg.tests.support import Sandbox, fixture_text, make_sandbox
from services.backup.walg.verify import VerifyOutputError, parse_verdict

ADMIN_URL = "postgresql://tester@/postgres?host=/sockets/meiva-pg-home&port=5433"


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    return make_sandbox(tmp_path, monkeypatch)


def _report(*, integrity: str, timeline: str = "OK") -> str:
    report: dict[str, Any] = json.loads(fixture_text("wal-verify-warning.json"))
    report["integrity"]["status"] = integrity
    report["timeline"]["status"] = timeline
    return json.dumps(report)


def test_segments_still_uploading_are_a_warning_not_a_failure() -> None:
    verdict = parse_verdict(fixture_text("wal-verify-warning.json"))

    assert (verdict.integrity, verdict.timeline) == ("WARNING", "OK")
    assert verdict.failed is False


def test_ok_is_not_a_failure() -> None:
    assert parse_verdict(_report(integrity="OK")).failed is False


@pytest.mark.parametrize(
    ("integrity", "timeline"), [("FAILURE", "OK"), ("OK", "FAILURE"), ("WARNING", "FAILURE")]
)
def test_a_failure_in_either_check_fails_the_chain(integrity: str, timeline: str) -> None:
    assert parse_verdict(_report(integrity=integrity, timeline=timeline)).failed is True


def test_an_exit_code_of_zero_with_a_failure_status_is_a_failed_chain(sandbox: Sandbox) -> None:
    sandbox.put("wal-verify.json", _report(integrity="FAILURE"))
    sandbox.put("wal-verify.rc", "0")

    verdict = verify.verify_chain(ADMIN_URL, path_reader=lambda: settings.walg.walg_config_file)

    assert verdict.failed is True
    assert sandbox.calls() == ["wal-verify integrity timeline --json"]


def test_a_non_zero_exit_is_an_error_even_with_an_ok_report(sandbox: Sandbox) -> None:
    sandbox.put("wal-verify.json", _report(integrity="OK"))
    sandbox.put("wal-verify.rc", "1")

    with pytest.raises(WalgCommandError):
        verify.verify_chain(ADMIN_URL, path_reader=lambda: settings.walg.walg_config_file)


@pytest.mark.parametrize("status", ["PASSED", "ok", "", None])
def test_an_unknown_status_is_an_error_never_a_pass(status: str | None) -> None:
    report: dict[str, Any] = json.loads(fixture_text("wal-verify-warning.json"))
    report["integrity"]["status"] = status

    with pytest.raises(VerifyOutputError):
        parse_verdict(json.dumps(report))


@pytest.mark.parametrize("text", ["", "not json", "{}", '{"integrity": {"status": "OK"}}', "[]"])
def test_output_without_both_checks_is_an_error(text: str) -> None:
    with pytest.raises(VerifyOutputError):
        parse_verdict(text)


def test_wal_verify_is_handed_the_owner_only_socket(sandbox: Sandbox) -> None:
    sandbox.put("wal-verify.json", _report(integrity="OK"))

    verify.verify_chain(ADMIN_URL, path_reader=lambda: settings.walg.walg_config_file)

    assert sandbox.verify_env_log() == ["PGHOST=/sockets/meiva-pg-home PGPORT=5433 PGUSER=tester"]


def _gap(
    start: str = "0000000100000061000000EB", end: str = "00000001000000690000006B"
) -> RecordedGap:
    return RecordedGap(
        start_segment=start,
        end_segment=end,
        segments=1921,
        reason="test",
        occurred_on="2026-10-17",
    )


def _missing(start: str, end: str, segments: int = 1921) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(fixture_text("wal-verify-warning.json"))
    report["integrity"]["status"] = "FAILURE"
    report["integrity"]["details"] = [
        {
            "timeline_id": 1,
            "start_segment": start,
            "end_segment": end,
            "segments_count": segments,
            "status": "MISSING_LOST",
        }
    ]
    return report


# meiva:note
# A recorded gap is an accepted decision, not a broken chain: the effective
# status turns OK while the raw one stays visible in the summary.
# meiva:link services/backup/walg/docs/walg-tick.meiva.okf.md
def test_a_recorded_gap_is_not_a_failure() -> None:
    verdict = parse_verdict(json.dumps(_missing(*_bounds())), recorded_gaps=(_gap(),))

    assert verdict.raw_integrity == "FAILURE"
    assert (verdict.integrity, verdict.timeline) == ("OK", "OK")
    assert verdict.failed is False
    assert [gap.occurred_on for gap in verdict.recorded] == ["2026-10-17"]
    assert "wal-verify said FAILURE" in verdict.summary()


def _bounds() -> tuple[str, str]:
    gap = _gap()
    return gap.start_segment, gap.end_segment


def test_a_gap_outside_the_record_still_fails_the_chain() -> None:
    verdict = parse_verdict(
        json.dumps(_missing("000000010000007100000001", "0000000100000071000000FF")),
        recorded_gaps=(_gap(),),
    )

    assert verdict.integrity == "FAILURE"
    assert verdict.failed is True
    assert verdict.unrecorded[0].start_segment == "000000010000007100000001"


def test_a_gap_wider_than_the_record_still_fails_the_chain() -> None:
    verdict = parse_verdict(
        json.dumps(_missing("000000010000006000000001", "00000001000000690000006B")),
        recorded_gaps=(_gap(),),
    )

    assert verdict.integrity == "FAILURE"
    assert verdict.failed is True


def test_the_packaged_record_names_the_rename_gap() -> None:
    gaps = load_recorded_gaps()

    assert gaps, "the packaged wal-gaps.json must carry the accepted gap"
    rename = next(gap for gap in gaps if gap.occurred_on == "2026-10-17")
    assert (rename.start_segment, rename.end_segment) == _bounds()
    assert rename.segments == 1921


def test_a_malformed_record_is_an_error_not_a_pass(tmp_path: Path) -> None:
    broken = tmp_path / "wal-gaps.json"
    broken.write_text('{"gaps": [{"start_segment": "nope"}]}', encoding="utf-8")

    with pytest.raises(RecordedGapError):
        load_recorded_gaps(broken)
