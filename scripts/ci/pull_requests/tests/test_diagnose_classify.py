from __future__ import annotations

import json

import pytest

from base.host.net.tests.fake_url_opener import FakeUrlOpener
from base.host.system.hostport import Host
from base.host.system.tests.fake_commands import FakeCommands, Reply, fake_host
from scripts.ci.pull_requests.diagnose import classify_check, diagnose_pr, print_diagnosis

# meiva:note
# These three strings are the ones a real 2026-10-22 run printed: the held-lock line, the
# cache verdict that used to read as a test failure, and a genuine test assertion. The
# classification is operator-facing, so what it says has to match what happened — a runner
# whose mirror is down must never read like a red test.
# meiva:link scripts/ci/pull_requests/docs/diagnostics.meiva.okf.md


def test_a_held_dpkg_lock_reads_as_infrastructure() -> None:
    log = (
        "apt lock: /var/lib/dpkg/lock-frontend is still held after 120.0s by pid(s) 5340 — "
        "that is a held lock, not a package or network failure"
    )

    classification, action = classify_check("Install Playwright system deps", log)

    assert classification.startswith("infrastructure:")
    assert "rerun" in action


def test_the_cache_verdict_from_a_lock_collision_reads_as_infrastructure() -> None:
    log = (
        "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 5340 (apt-get)\n"
        "::error::the cached archives cannot satisfy the system deps"
    )

    classification, _ = classify_check("Install Playwright system deps", log)

    assert classification.startswith("infrastructure:")


def test_a_real_test_failure_is_not_classified_as_infrastructure() -> None:
    classification, _ = classify_check(
        "e2e (Playwright happy path)", "AssertionError: expected 200, got 500"
    )

    assert not classification.startswith("infrastructure:")


def _diagnosis_host(view: object, pulls: object) -> Host:
    runner = FakeCommands()
    runner.on(
        "gh",
        Reply(stdout=json.dumps(view)),
        Reply(stdout="abc123"),
        Reply(stdout='{"workflow_runs": []}'),
        Reply(stdout=json.dumps(pulls)),
    )
    return fake_host(runner)


@pytest.mark.parametrize("view", [None, [], True, "invalid"])
def test_invalid_pr_view_remains_a_best_effort_error(view: object) -> None:
    result = diagnose_pr(
        "42", "o/r", token=None, host=_diagnosis_host(view, []), open_url=FakeUrlOpener()
    )
    assert "gh_error" in result
    assert result["gh_error"] == "gh pr view failed"
    assert result["checks"] == []


@pytest.mark.parametrize("pulls", [None, {}, True, "invalid", [None, 7, "pull"]])
def test_malformed_synthetic_prs_do_not_interrupt_diagnosis(pulls: object) -> None:
    result = diagnose_pr(
        "42",
        "o/r",
        token=None,
        host=_diagnosis_host({"state": "MERGED", "statusCheckRollup": []}, pulls),
        open_url=FakeUrlOpener(),
    )
    assert result["synthetic_test_prs"] == []
    assert "state" in result
    assert result["state"] == "MERGED"
    assert "gh_error" not in result


def test_valid_synthetic_pr_survives_malformed_siblings() -> None:
    pull = {"number": 43, "state": "closed", "head": {"ref": "trunk-merge/pr-42/attempt"}}
    pulls: list[object] = [None, {"head": []}, pull]
    result = diagnose_pr(
        "42",
        "o/r",
        token=None,
        host=_diagnosis_host({"state": "MERGED"}, pulls),
        open_url=FakeUrlOpener(),
    )
    assert result["synthetic_test_prs"] == [
        {"number": 43, "state": "closed", "head_ref": "trunk-merge/pr-42/attempt"}
    ]


def test_invalid_rollup_conclusions_keep_valid_check_classification(
    capsys: pytest.CaptureFixture[str],
) -> None:
    view: dict[str, object] = {
        "state": "MERGED",
        "statusCheckRollup": [
            {"name": "bad list", "conclusion": []},
            {"name": "bad mapping", "conclusion": {}},
            {"name": "lint rules", "conclusion": "FAILURE"},
        ],
    }
    result = diagnose_pr(
        "42", "o/r", token=None, host=_diagnosis_host(view, []), open_url=FakeUrlOpener()
    )
    assert len(result["checks"]) == 1
    assert result["checks"][0]["check"] == "lint rules"
    assert result["checks"][0]["classification"] == "deterministic lint failure"
    print_diagnosis(result)
    assert "lint rules: deterministic lint failure" in capsys.readouterr().out
