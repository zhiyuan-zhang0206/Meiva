from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).resolve().parents[5]
ENTRY = ".venv/bin/python scripts/lint/diagnostics/diff_hints/hints.py"


def _hooks() -> dict[str, dict[str, Any]]:
    config = cast(dict[str, Any], yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text()))
    return {hook["id"]: hook for repo in config["repos"] for hook in repo["hooks"]}


def test_a_plain_install_puts_the_commit_msg_hook_in_place() -> None:
    config = cast(dict[str, Any], yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text()))
    assert "commit-msg" in config["default_install_hook_types"]


def test_the_commit_stage_reads_the_message_file_pre_commit_appends() -> None:
    hook = _hooks()["diff-hints-commit-msg"]
    assert hook["stages"] == ["commit-msg"]
    assert hook["entry"] == f"{ENTRY} commit-msg"
    assert hook.get("pass_filenames", True) is True


def test_the_push_stage_judges_the_branch_whatever_files_pre_commit_selects() -> None:
    hook = _hooks()["diff-hints-branch"]
    assert hook["stages"] == ["pre-push"]
    assert hook["entry"] == f"{ENTRY} branch"
    assert hook["always_run"] is True
    assert hook["pass_filenames"] is False
