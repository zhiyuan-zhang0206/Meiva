#!/usr/bin/env python3
# meiva:note
# Compare GitHub's live branch protections and active CI workflows with Meiva's required PR
# admission gate (`scripts/audit/analysis/protection.py`), distinguishing configuration drift from
# an API or tool failure.
# meiva:link scripts/audit/docs/audit.meiva.okf.md

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

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

_REPO_ROOT = Path(__file__).resolve().parents[2]


# meiva:note
# The origin remote of this checkout, as git prints it; naming the repository is the pure
# `analysis/protection.audit_target`.
def _origin_url() -> str:
    completed = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def run_gh(args: list[str]) -> str:
    completed = subprocess.run(  # noqa: S603
        ["gh", *args],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout


def _json_mapping(text: str, location: str) -> dict[str, object]:
    payload: object = json.loads(text)
    return mapping(payload, location)


def _audit(
    repo: str,
    branch: str,
    run_gh_call: Callable[[list[str]], str],
) -> list[str]:
    expected = expected_checks()
    protection = _json_mapping(
        run_gh_call(
            [
                "api",
                "--method",
                "GET",
                f"repos/{repo}/branches/{encode_branch(branch)}/protection",
            ]
        ),
        "branch protection response",
    )
    workflows = _json_mapping(
        run_gh_call(
            [
                "api",
                "--method",
                "GET",
                f"repos/{repo}/actions/workflows",
            ]
        ),
        "workflows response",
    )
    return drift_report(protection, expected, workflows)


def main(
    argv: list[str] | None = None,
    *,
    run_gh_fn: Callable[[list[str]], str] = run_gh,
) -> int:
    parser = argparse.ArgumentParser(
        description="Compare GitHub branch protection with Meiva's required PR admission checks."
    )
    parser.add_argument("--repo", help="GitHub repository as owner/repo")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    try:
        remote_url = _origin_url() if args.repo is None else None
        repo = audit_target(args.repo, args.branch, remote_url)
        findings = _audit(repo, args.branch, run_gh_fn)
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        subprocess.CalledProcessError,
    ) as error:
        print(
            report_text(
                "error",
                [error_detail(getattr(error, "stderr", None), str(error))],
                as_json=args.as_json,
            )
        )
        return 2

    result, code = outcome(findings)
    print(report_text(result, findings, as_json=args.as_json))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
