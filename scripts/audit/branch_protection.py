#!/usr/bin/env python3
"""Compare GitHub's live branch protections with Meiva's Trunk merge-queue declaration.

The repository declares its merge gate in ``.trunk/trunk.yaml`` (queue testing
gate) plus the admission gate ruled in P2 (three suite aggregators),
amended 2026-09-13 (task #3207) with the docs-only gate
``doc lints (pre-commit family)``; GitHub stores required checks and workflow
activation outside git. This read-only audit makes that external state observable and
distinguishes drift from an API/tool failure.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

from meizel import lint_common
from scripts.audit.analysis import (
    audit_target,
    drift_report,
    encode_branch,
    error_detail,
    expected_checks,
    mapping,
    outcome,
    report_text,
    trunk_gate_findings,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
TRUNK_FILE = _REPO_ROOT / ".trunk" / "trunk.yaml"


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
    """Run one read-only ``gh`` invocation and return its stdout."""
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
    *,
    trunk_file: Path = TRUNK_FILE,
    run_gh_fn: Callable[[list[str]], str] = run_gh,
) -> list[str]:
    trunk_text = lint_common.read_utf8_text(trunk_file)
    if trunk_text is None:
        raise FileNotFoundError(trunk_file)
    declaration_findings = trunk_gate_findings(trunk_text)
    if declaration_findings:
        return declaration_findings
    expected = expected_checks()
    protection = _json_mapping(
        run_gh_fn(
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
        run_gh_fn(
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
    trunk_file: Path = TRUNK_FILE,
    run_gh_fn: Callable[[list[str]], str] = run_gh,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="GitHub repository as owner/repo")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    try:
        remote_url = _origin_url() if args.repo is None else None
        repo = audit_target(args.repo, args.branch, remote_url)
        findings = _audit(repo, args.branch, trunk_file=trunk_file, run_gh_fn=run_gh_fn)
    except (
        FileNotFoundError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        yaml.YAMLError,
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
