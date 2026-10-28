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
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import yaml

from meizel import lint_common
from scripts.audit.analysis import (
    drift_report,
    encode_branch,
    expected_checks,
    mapping,
    trunk_gate_findings,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
TRUNK_FILE = _REPO_ROOT / ".trunk" / "trunk.yaml"
_GITHUB_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_REMOTE_REPO = re.compile(r"(?:github\.com[:/])([^/]+)/([^/]+?)(?:\.git)?$")


def derive_repo() -> str:
    """Derive ``owner/repo`` from this checkout's origin remote."""
    completed = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    match = _REMOTE_REPO.search(completed.stdout.strip())
    if match is None:
        raise ValueError("origin is not a GitHub repository URL")
    return f"{match.group(1)}/{match.group(2)}"


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
    if _GITHUB_REPO.fullmatch(repo) is None:
        raise ValueError("--repo must have the form owner/repo")
    if not branch:
        raise ValueError("--branch must not be empty")

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


def _print_report(result: str, findings: list[str], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"result": result, "findings": findings}, sort_keys=True))
    elif result == "ok":
        print("Branch protection audit: no drift detected.")
    else:
        print(f"Branch protection audit: {result}.")
        for finding in findings:
            print(f"- {finding}")
    print(f"AUDIT_RESULT={result}")


def _error_detail(error: BaseException) -> str:
    if isinstance(error, subprocess.CalledProcessError):
        stderr = error.stderr.strip() if isinstance(error.stderr, str) else ""
        return stderr or str(error)
    return str(error)


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
        repo = args.repo if args.repo is not None else derive_repo()
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
        _print_report("error", [_error_detail(error)], as_json=args.as_json)
        return 2

    if findings:
        _print_report("drift", findings, as_json=args.as_json)
        return 1
    _print_report("ok", [], as_json=args.as_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
