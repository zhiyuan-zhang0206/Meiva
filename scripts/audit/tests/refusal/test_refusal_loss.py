from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.host.proc import run_bounded
from scripts.audit.analysis.refusal_loss import (
    DELETION,
    DROPPED,
    UNREPLACED,
    findings_for,
    manifest_delta,
    parse_diff,
    refusal_messages,
)

REPO_ROOT = Path(__file__).parents[4]
GATE = Path(__file__).parents[2] / "refusal_loss.py"

GUARD_DIFF = """diff --git a/base/cluster/postgres.py b/base/cluster/postgres.py
--- a/base/cluster/postgres.py
+++ b/base/cluster/postgres.py
@@ -80,4 +80,3 @@ def boot_id() -> str:
-    if sys.platform not in {"linux", "darwin"}:
-        raise RuntimeError("owned PostgreSQL requires a supported POSIX platform")
+    Platform.current()
     boot = native_boot_id()
"""

CONTINUED_RAISE_DIFF = """diff --git a/base/agents/history/checkpoint_postgres_walks.py b/base/agents/history/checkpoint_postgres_walks.py
--- a/base/agents/history/checkpoint_postgres_walks.py
+++ b/base/agents/history/checkpoint_postgres_walks.py
@@ -10,4 +10,0 @@ def validate() -> None:
-        raise RuntimeError(
-            "checkpoint-postgres adapter requires the pinned release"
-        )
-    boot = native_boot_id()
"""

DEFAULT_DIFF = """diff --git a/cli/database/lifecycle_audit.py b/cli/database/lifecycle_audit.py
--- a/cli/database/lifecycle_audit.py
+++ b/cli/database/lifecycle_audit.py
@@ -120,4 +120,6 @@ def operator_lifecycle_audit() -> None:
-    except OSError as exc:
-        raise AuditUnavailableError("lifecycle audit cannot read the caller block")
+    except Exception as exc:
+        print(f"warning: {exc!r}")
+        return None
"""


def test_parse_diff_keeps_each_hunk_with_its_path() -> None:
    hunks = parse_diff(GUARD_DIFF + DEFAULT_DIFF)
    assert [hunk.path for hunk in hunks] == [
        "base/cluster/postgres.py",
        "cli/database/lifecycle_audit.py",
    ]
    assert hunks[0].added == ("    Platform.current()",)
    assert hunks[1].removed[1].strip().startswith("raise AuditUnavailableError")


def test_a_removed_raise_is_read_across_the_lines_its_parentheses_leave_open() -> None:
    hunks = parse_diff(CONTINUED_RAISE_DIFF)
    assert refusal_messages(hunks[0]) == (
        "checkpoint-postgres adapter requires the pinned release",
    )


def test_manifest_delta_is_positive_when_a_commit_deletes_more_debt_than_it_adds() -> None:
    assert manifest_delta("3\t7\tbase/cluster/meizel.toml\n1\t0\tbase/x/meizel.toml") == 3
    assert manifest_delta("9\t2\tbase/cluster/meizel.toml") == -7


def test_a_guard_replaced_by_a_call_is_listed_but_not_blocking_under_deletion() -> None:
    hunks = parse_diff(GUARD_DIFF)
    found = findings_for(
        commit="1a2c99a54",
        subject="boot id",
        hunks=hunks,
        tree_text="",
        existing=frozenset({"base/cluster/postgres.py"}),
        rule=DELETION,
    )
    assert [(finding.lens, finding.blocking) for finding in found] == [(DROPPED, False)]
    wide = findings_for(
        commit="1a2c99a54",
        subject="boot id",
        hunks=hunks,
        tree_text="",
        existing=frozenset({"base/cluster/postgres.py"}),
        rule=UNREPLACED,
    )
    assert [finding.blocking for finding in wide] == [True]


def test_a_pure_deletion_of_a_refusal_blocks_and_its_message_survives_elsewhere() -> None:
    hunks = parse_diff(CONTINUED_RAISE_DIFF)
    existing = frozenset({"base/agents/history/checkpoint_postgres_walks.py"})
    blocked = findings_for(
        commit="1c7e410fe",
        subject="dependency pin",
        hunks=hunks,
        tree_text="",
        existing=existing,
    )
    assert [finding.blocking for finding in blocked] == [True]
    moved = findings_for(
        commit="1c7e410fe",
        subject="dependency pin",
        hunks=hunks,
        tree_text="raise RuntimeError('checkpoint-postgres adapter requires the pinned release')",
        existing=existing,
    )
    assert moved == ()


def test_a_hunk_that_adds_a_default_is_listed_without_blocking() -> None:
    found = findings_for(
        commit="781cc56fd",
        subject="audit sink",
        hunks=parse_diff(DEFAULT_DIFF),
        tree_text="",
        existing=frozenset({"cli/database/lifecycle_audit.py"}),
    )
    assert [(finding.lens, finding.blocking) for finding in found] == [
        (DROPPED, True),
        ("default-allow", False),
    ]


def test_a_test_file_or_a_deleted_module_carries_no_finding() -> None:
    test_hunk = parse_diff(GUARD_DIFF.replace("base/cluster/postgres.py", "base/tests/test_x.py"))
    deleted = parse_diff(GUARD_DIFF)
    assert (
        findings_for(
            commit="abc",
            subject="s",
            hunks=test_hunk,
            tree_text="",
            existing=frozenset({"base/tests/test_x.py"}),
        )
        == ()
    )
    assert (
        findings_for(commit="abc", subject="s", hunks=deleted, tree_text="", existing=frozenset())
        == ()
    )


@pytest.fixture
def debt_checkout(tmp_path: Path) -> Path:
    run_bounded(["git", "init", "--quiet"], timeout=30, capture_output=True, cwd=tmp_path)
    run_bounded(
        ["git", "config", "user.email", "audit@example.invalid"],
        timeout=30,
        capture_output=True,
        cwd=tmp_path,
    )
    run_bounded(
        ["git", "config", "user.name", "Audit"], timeout=30, capture_output=True, cwd=tmp_path
    )
    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "meizel.toml").write_text(
        '[debt.first-party-patch]\n"a.py#x" = 3\n"b.py#y" = 2\n', encoding="utf-8"
    )
    (tmp_path / "base" / "boot.py").write_text(
        'def boot() -> None:\n    raise RuntimeError("the owned database needs a POSIX host")\n'
        "\n\ndef other() -> None:\n    return None\n",
        encoding="utf-8",
    )
    run_bounded(["git", "add", "."], timeout=30, capture_output=True, cwd=tmp_path)
    run_bounded(["git", "commit", "-qm", "base"], timeout=30, capture_output=True, cwd=tmp_path)
    return tmp_path


def _commit(debt_checkout: Path, *, source: str, debt: str, message: str) -> None:
    (debt_checkout / "base" / "boot.py").write_text(source, encoding="utf-8")
    (debt_checkout / "base" / "meizel.toml").write_text(debt, encoding="utf-8")
    run_bounded(["git", "add", "."], timeout=30, capture_output=True, cwd=debt_checkout)
    run_bounded(
        ["git", "commit", "-qm", message], timeout=30, capture_output=True, cwd=debt_checkout
    )


def _run_gate(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # meiva:note
    # The gate as its operator runs it: a command that reads the checkout it stands in and
    # answers with its exit code and its report.
    return run_bounded(
        [sys.executable, str(GATE), *args],
        timeout=120,
        capture_output=True,
        cwd=repo,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )


def test_the_gate_fails_a_debt_reducing_commit_that_deletes_a_refusal(
    debt_checkout: Path,
) -> None:
    _commit(
        debt_checkout,
        source="def other() -> None:\n    return None\n",
        debt='[debt.first-party-patch]\n"a.py#x" = 1\n',
        message="pay debt down and drop the guard",
    )
    assert _run_gate(debt_checkout, "--base", "HEAD~1").returncode == 1


def test_the_gate_lists_a_refusal_replaced_by_a_default_unless_asked_to_block(
    debt_checkout: Path,
) -> None:
    _commit(
        debt_checkout,
        source=(
            "def boot() -> None:\n    return None\n\n\ndef other() -> None:\n    return None\n"
        ),
        debt='[debt.first-party-patch]\n"a.py#x" = 1\n',
        message="pay debt down and soften the guard",
    )
    assert _run_gate(debt_checkout, "--base", "HEAD~1").returncode == 0
    assert _run_gate(debt_checkout, "--base", "HEAD~1", "--blocking", "unreplaced").returncode == 1


def test_the_gate_skips_a_commit_that_pays_no_debt_down(debt_checkout: Path) -> None:
    _commit(
        debt_checkout,
        source=(
            'def boot() -> None:\n    raise ValueError("a different refusal")\n'
            "\n\ndef other() -> None:\n    return None\n"
        ),
        debt='[debt.first-party-patch]\n"a.py#x" = 1\n',
        message="reword",
    )
    assert _run_gate(debt_checkout, "--base", "HEAD~1").returncode == 0


def test_the_json_report_carries_every_finding_as_an_object(debt_checkout: Path) -> None:
    _commit(
        debt_checkout,
        source="def other() -> None:\n    return None\n",
        debt='[debt.first-party-patch]\n"a.py#x" = 1\n',
        message="pay debt down and drop the guard",
    )
    reported = json.loads(_run_gate(debt_checkout, "--base", "HEAD~1", "--json").stdout)
    assert [(entry["lens"], entry["blocking"], entry["path"]) for entry in reported] == [
        ("dropped-refusal", True, "base/boot.py")
    ]
