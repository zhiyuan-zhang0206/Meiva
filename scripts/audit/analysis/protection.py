# meiva:note
# What the branch-protection audit promises: the admission gate, the queue gate, live drift.
#
# `branch_protection.py` runs `gh`, reads `.trunk/trunk.yaml` and prints; which checks must be
# required, which workflow files must be active, and how a GitHub payload differs from that
# contract are decided here.

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import TypeGuard

import yaml

__all__ = ["drift_report", "encode_branch", "expected_checks", "mapping", "trunk_gate_findings"]

# meiva:note
# P2 ruling (2026-09-02): the branch-protection admission gate is the three
# suite aggregators. Amended 2026-09-13 (task #3207): the docs-only gate
# "doc lints (pre-commit family)" joined the admission set once the docs-only
# classify gap was closed by #2302 and the check was promoted to a required
# context. The queue TESTING gate lives in trunk.yaml and is audited separately
# by trunk_gate_findings().
_ADMISSION_CHECKS = frozenset(
    {
        "backend (pytest + pyright)",
        "frontend (eslint + tsc + vitest)",
        "e2e (Playwright happy path)",
        "doc lints (pre-commit family)",
    }
)
_MATRIX_TEMPLATE = re.compile(r"\$\{\{ matrix")
_REQUIRED_WORKFLOWS = (
    ".github/workflows/ci.yml",
    ".github/workflows/ci-rerun.yml",
)
_SAFE_URL_BYTES = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~")


def encode_branch(branch: str) -> str:
    # meiva:note
    # The branch name as a URL path segment: anything outside the safe set is percent-encoded.
    return "".join(
        chr(byte) if byte in _SAFE_URL_BYTES else f"%{byte:02X}" for byte in branch.encode("utf-8")
    )


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_raw_dict(value) and all(isinstance(key, str) for key in value)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_str_list(value: object) -> TypeGuard[list[str]]:
    return _is_object_list(value) and all(isinstance(item, str) for item in value)


def mapping(value: object, location: str) -> dict[str, object]:
    # meiva:note
    # A JSON object with string keys, or TypeError naming `location`.
    if not _is_raw_dict(value):
        raise TypeError(f"{location} must be a mapping")
    if not _is_str_dict(value):
        raise TypeError(f"{location} keys must be strings")
    return value


def _list(value: object, location: str) -> list[object]:
    if not _is_object_list(value):
        raise TypeError(f"{location} must be a list")
    return value


def expected_checks() -> frozenset[str]:
    # meiva:note
    # Return the branch-protection admission gate (P2 ruling).
    return _ADMISSION_CHECKS


def trunk_gate_findings(text: str) -> list[str]:
    # meiva:note
    # Audit the queue TESTING gate declared in .trunk/trunk.yaml.
    #
    # Invariants: required_statuses must be present and non-empty, carry the three suite
    # aggregators, and must not contain raw matrix template names (a fully-skipped matrix
    # reports only the template name, so such an entry could stall the queue forever on
    # docs-only PRs).
    document: object = yaml.safe_load(text)
    root = mapping(document, ".trunk/trunk.yaml")
    if "merge" not in root:
        return ["trunk.yaml is missing merge.required_statuses"]
    merge = mapping(root["merge"], "merge")
    if "required_statuses" not in merge:
        return ["trunk.yaml is missing merge.required_statuses"]
    statuses = _string_set(merge["required_statuses"], "merge.required_statuses")
    if not statuses:
        return ["trunk.yaml merge.required_statuses must not be empty"]

    findings: list[str] = []
    for aggregator in (
        "backend (pytest + pyright)",
        "frontend (eslint + tsc + vitest)",
        "e2e (Playwright happy path)",
    ):
        if aggregator not in statuses:
            findings.append(f"trunk.yaml required_statuses missing {aggregator}")
    for name in sorted(statuses):
        if _MATRIX_TEMPLATE.search(name):
            findings.append(f"trunk.yaml required_statuses contains a matrix template name: {name}")
    return findings


def _string_set(value: object, location: str) -> frozenset[str]:
    items = _list(value, location)
    if not _is_str_list(items):
        raise TypeError(f"{location} entries must be strings")
    return frozenset(items)


def _boolean(value: object, location: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{location} must be a boolean")
    return value


def drift_report(
    protection_payload: Mapping[str, object],
    expected: frozenset[str],
    workflows_payload: Mapping[str, object],
) -> list[str]:
    # meiva:note
    # Return human-readable differences from the branch-protection contract.
    findings: list[str] = []

    if (
        "required_status_checks" not in protection_payload
        or protection_payload["required_status_checks"] is None
    ):
        findings.append("branch protection has no required_status_checks")
    else:
        status_checks = mapping(
            protection_payload["required_status_checks"], "required_status_checks"
        )
        contexts = _string_set(status_checks["contexts"], "required_status_checks.contexts")
        missing = expected - contexts
        extra = contexts - expected
        if missing:
            findings.append("missing required status checks: " + ", ".join(sorted(missing)))
        if extra:
            findings.append("extra required status checks: " + ", ".join(sorted(extra)))
        if _boolean(status_checks["strict"], "required_status_checks.strict"):
            findings.append("required status checks must have strict=false")

    enforce_admins = mapping(protection_payload["enforce_admins"], "enforce_admins")
    if not _boolean(enforce_admins["enabled"], "enforce_admins.enabled"):
        findings.append("branch protection must enforce rules for administrators")

    workflow_entries = _list(workflows_payload["workflows"], "workflows")
    live_workflows: dict[str, str] = {}
    for index, value in enumerate(workflow_entries):
        workflow = mapping(value, f"workflows[{index}]")
        path = workflow["path"]
        state = workflow["state"]
        if not isinstance(path, str) or not isinstance(state, str):
            raise TypeError(f"workflows[{index}].path and state must be strings")
        if path in live_workflows:
            raise ValueError(f"duplicate workflow path in GitHub response: {path}")
        live_workflows[path] = state

    for path in _REQUIRED_WORKFLOWS:
        name = Path(path).name
        if path not in live_workflows:
            findings.append(f"workflow {name} is missing on GitHub")
        elif live_workflows[path] != "active":
            findings.append(f"workflow {name} is not active (state={live_workflows[path]})")

    return findings
