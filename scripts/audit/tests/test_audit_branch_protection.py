from __future__ import annotations

import json
from typing import NoReturn

import pytest

from scripts.audit import branch_protection as audit
from scripts.audit.analysis import (
    audit_target,
    drift_report,
    encode_branch,
    error_detail,
    expected_checks,
    mapping,
    outcome,
    report_text,
)

_ADMISSION_CHECKS = frozenset(
    {
        "backend (pytest + pyright)",
        "frontend (eslint + tsc + vitest)",
        "e2e (Playwright happy path)",
        "doc lints (pre-commit family)",
    }
)
_EXPECTED_CHECKS = _ADMISSION_CHECKS


def _protection(
    contexts: list[str] | None = None,
    *,
    strict: bool = False,
    enforce_admins: bool = True,
) -> dict[str, object]:
    return {
        "required_status_checks": {
            "contexts": list(_EXPECTED_CHECKS) if contexts is None else contexts,
            "strict": strict,
        },
        "enforce_admins": {"enabled": enforce_admins},
    }


def _workflows(*, ci_state: str = "active", rerun_state: str = "active") -> dict[str, object]:
    return {
        "workflows": [
            {"path": ".github/workflows/ci.yml", "state": ci_state},
            {"path": ".github/workflows/ci-rerun.yml", "state": rerun_state},
        ]
    }


def _fake_gh(protection: dict[str, object], workflows: dict[str, object]):
    def fake(args: list[str]) -> str:
        endpoint = args[-1]
        if endpoint.endswith("/protection"):
            return json.dumps(protection)
        if endpoint.endswith("/actions/workflows"):
            return json.dumps(workflows)
        raise AssertionError(f"unexpected gh API endpoint: {endpoint}")

    return fake


def test_expected_checks_is_the_branch_admission_gate() -> None:
    assert expected_checks() == _ADMISSION_CHECKS
    assert "doc lints (pre-commit family)" in _ADMISSION_CHECKS


def test_drift_report_accepts_the_exact_live_contract() -> None:
    assert drift_report(_protection(), _EXPECTED_CHECKS, _workflows()) == []


def test_drift_report_names_missing_contexts() -> None:
    contexts = sorted(_EXPECTED_CHECKS - {"backend (pytest + pyright)"})
    assert drift_report(_protection(contexts), _EXPECTED_CHECKS, _workflows()) == [
        "missing required status checks: backend (pytest + pyright)"
    ]


def test_drift_report_names_extra_contexts() -> None:
    contexts = [*_EXPECTED_CHECKS, "legacy"]
    assert drift_report(_protection(contexts), _EXPECTED_CHECKS, _workflows()) == [
        "extra required status checks: legacy"
    ]


def test_drift_report_rejects_strict_status_checks() -> None:
    assert drift_report(_protection(strict=True), _EXPECTED_CHECKS, _workflows()) == [
        "required status checks must have strict=false"
    ]


def test_drift_report_requires_admin_enforcement() -> None:
    assert drift_report(_protection(enforce_admins=False), _EXPECTED_CHECKS, _workflows()) == [
        "branch protection must enforce rules for administrators"
    ]


def test_drift_report_detects_missing_required_status_checks() -> None:
    protection = {"enforce_admins": {"enabled": True}}
    assert drift_report(protection, _EXPECTED_CHECKS, _workflows()) == [
        "branch protection has no required_status_checks"
    ]


def test_drift_report_detects_a_missing_workflow() -> None:
    workflows = {"workflows": [{"path": ".github/workflows/ci.yml", "state": "active"}]}
    assert drift_report(_protection(), _EXPECTED_CHECKS, workflows) == [
        "workflow ci-rerun.yml is missing on GitHub"
    ]


def test_drift_report_detects_an_inactive_workflow() -> None:
    assert drift_report(
        _protection(), _EXPECTED_CHECKS, _workflows(ci_state="disabled_manually")
    ) == ["workflow ci.yml is not active (state=disabled_manually)"]


def test_cli_reports_ok_and_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        audit.main(
            ["--repo", "owner/repo", "--json"],
            run_gh_fn=_fake_gh(_protection(), _workflows()),
        )
        == 0
    )
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=ok")


def test_cli_reports_drift_and_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    protection = _protection(sorted(_EXPECTED_CHECKS - {"e2e (Playwright happy path)"}))

    assert (
        audit.main(
            ["--repo", "owner/repo", "--json"],
            run_gh_fn=_fake_gh(protection, _workflows()),
        )
        == 1
    )
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=drift")


def test_cli_reports_verification_errors_and_exits_two(
    capsys: pytest.CaptureFixture[str],
) -> None:

    def missing_gh(_args: list[str]) -> NoReturn:
        raise FileNotFoundError("gh")

    assert audit.main(["--repo", "owner/repo", "--json"], run_gh_fn=missing_gh) == 2
    assert capsys.readouterr().out.rstrip().endswith("AUDIT_RESULT=error")


def test_encode_branch_percent_encodes_everything_outside_the_safe_set() -> None:
    assert encode_branch("feature/x y") == "feature%2Fx%20y"
    assert encode_branch("main") == "main"


def test_mapping_refuses_a_payload_that_is_not_an_object() -> None:
    with pytest.raises(TypeError, match="branch protection response must be a mapping"):
        mapping([], "branch protection response")


def test_audit_target_names_the_repository_and_checks_both_inputs() -> None:
    assert audit_target("owner/repo", "main", None) == "owner/repo"
    assert audit_target(None, "main", "git@github.com:owner/repo.git") == "owner/repo"
    assert audit_target(None, "release/1.0", "https://github.com/owner/repo") == "owner/repo"
    with pytest.raises(ValueError, match="--repo must have the form owner/repo"):
        audit_target("not-a-repository", "main", None)
    with pytest.raises(ValueError, match="--branch must not be empty"):
        audit_target("owner/repo", "", None)
    with pytest.raises(ValueError, match="origin is not a GitHub repository URL"):
        audit_target(None, "main", "https://example.com/owner/repo")


def test_report_text_says_the_same_thing_as_json_text_and_findings() -> None:
    assert report_text("ok", [], as_json=False) == (
        "Branch protection audit: no drift detected.\nAUDIT_RESULT=ok"
    )
    assert report_text("drift", ["one", "two"], as_json=False) == (
        "Branch protection audit: drift.\n- one\n- two\nAUDIT_RESULT=drift"
    )
    assert report_text("drift", ["one"], as_json=True) == (
        '{"findings": ["one"], "result": "drift"}\nAUDIT_RESULT=drift'
    )


def test_outcome_turns_findings_into_the_reported_verdict_and_exit_status() -> None:
    assert outcome([]) == ("ok", 0)
    assert outcome(["drift"]) == ("drift", 1)


def test_error_detail_prefers_a_child_stderr_over_its_message() -> None:
    assert error_detail("boom\n", "CalledProcessError: gh") == "boom"
    assert error_detail(None, "FileNotFoundError: gh") == "FileNotFoundError: gh"
    assert error_detail("  ", "timed out") == "timed out"
