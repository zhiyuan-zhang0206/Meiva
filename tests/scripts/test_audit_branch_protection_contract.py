from __future__ import annotations

from pathlib import Path

import yaml

from scripts.audit.analysis import trunk_gate_findings

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_trunk_gate_findings_accept_the_real_declaration() -> None:
    text = (_REPO_ROOT / ".trunk" / "trunk.yaml").read_text()
    assert trunk_gate_findings(text) == []


def test_real_trunk_yaml_declares_the_full_gate() -> None:
    text = (_REPO_ROOT / ".trunk" / "trunk.yaml").read_text()
    document = yaml.safe_load(text)
    statuses = document["merge"]["required_statuses"]
    assert len(statuses) == 12
    assert "secret scan (Gitleaks)" in statuses
    assert "no silent resurrection" not in statuses
    assert "qa-approved-gate" not in statuses
    assert "backend (pytest + pyright)" in statuses
    assert "frontend (eslint + tsc + vitest)" in statuses
    assert "e2e (Playwright happy path)" in statuses
    assert not any("${{" in name for name in statuses)
