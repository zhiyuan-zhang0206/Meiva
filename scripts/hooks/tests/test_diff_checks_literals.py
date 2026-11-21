# meiva:note
# What deleted-literal scanning under `--skip-tests` promises: distinctive literals and backtick
# identifiers removed from production hunks catch transitive literal-assertion tests (such as PR
# #9123 / `9db9842748`), auto-run within `MAX_LITERAL_AUTO_TESTS` and fail closed on red runs,
# refuse to launch a wide local pytest sweep past the cap, obey `--skip-literal-tests`, and incur
# zero pytest overhead when nothing matches.

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from scripts.hooks.diff_checks import (
    MAX_LITERAL_AUTO_TESTS,
    ChangeSet,
    Selection,
    build_receipt,
    check,
    diff_text_for,
    extract_deleted_literals,
    parse_args,
    plan,
    run_child,
)
from scripts.hooks.diff_checks_literals import (
    candidate_test_files,
    find_literal_test_hits,
    is_pytest_file,
    is_test_path,
)

_PYTHON = sys.executable

# meiva:note
# Faithful diff excerpt modeled on PR #9123 (`9db9842748`): `meiva.agents.send_message` moved out
# of `_CLAUDE_CODE_VISIBILITY_WHY` into `_USER_ATTENTION_RULE` (two separate hunks) while
# `"When you have a delegator"` was removed from the visibility copy.
_PR_9123_STYLE_DIFF = """\
diff --git a/agent/graph/prompt/conversation.py b/agent/graph/prompt/conversation.py
index 4e5d145fa5..4d14f7a440 100644
--- a/agent/graph/prompt/conversation.py
+++ b/agent/graph/prompt/conversation.py
@@ -24,1 +26,2 @@ _USER_ATTENTION_RULE = (
-    "progress and results to the agreed delegator; user-only decisions reach "
+    "progress and results to the agreed delegator via `meiva.agents.send_message`; "
+    "user-only decisions reach "
@@ -44,7 +47,4 @@ _USER_ATTENTION_RULE = (
-    "Unlike standalone Claude Code, where output goes to a human at a terminal, many Meiva "
-    "agents are workers spawned by another agent to report via `meiva.agents.send_message`. "
-    "When you have a delegator, reporting there completes delivery — even if your brief quotes "
+    "Unlike standalone Claude Code, where output goes to a human at a terminal, a Meiva "
+    "worker reports to its delegator. The delegated visibility rule above still applies "
"""


def _write_report(argv: Sequence[str], *, passed: int, failed: int = 0, errors: int = 0) -> None:
    named = next(part for part in argv if part.startswith("--junitxml="))
    cases = "".join(f'<testcase classname="t" name="p{i}" />' for i in range(passed))
    cases += "".join(
        f'<testcase classname="t" name="f{i}"><failure /></testcase>' for i in range(failed)
    )
    cases += "".join(
        f'<testcase classname="t" name="e{i}"><error /></testcase>' for i in range(errors)
    )
    Path(named.split("=", 1)[1]).write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="{passed + failed + errors}">'
        f"{cases}</testsuite></testsuites>",
        encoding="utf-8",
    )


def _git(repo: Path, *args: str) -> str:
    code, output, _ = run_child(
        ("git", *args),
        repo,
        environment={"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
    )
    assert code == 0, output
    return output.strip()


def _init_git_with_base(repo: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", base_sha)


def test_extract_deleted_literals_captures_pr9123_moved_backtick_and_deleted_clauses() -> None:
    literals = extract_deleted_literals(_PR_9123_STYLE_DIFF)

    assert "meiva.agents.send_message" in literals, (
        "moving a backtick identifier to a different hunk still breaks tests asserting on the old "
        "section; per-hunk subtraction must preserve it"
    )
    assert "When you have a delegator" in literals
    assert "Unlike standalone Claude Code" not in literals, (
        "text still present in the same hunk's added lines was not removed from that location"
    )


def test_extract_deleted_literals_ignores_headers_comments_tests_and_low_signal_words() -> None:
    diff = """\
diff --git a/pkg/tests/test_mod.py b/pkg/tests/test_mod.py
--- a/pkg/tests/test_mod.py
+++ b/pkg/tests/test_mod.py
@@ -1,2 +1,1 @@
-assert "should_never_be_extracted_from_test_file" in out
+assert True
diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,7 +1,3 @@
-# comment mentioning `comment_only_symbol` and "comment_only_literal"
-encoding = "utf-8"
-short = "new message"
-kept = "kept_in_same_hunk_literal"
-removed = f"distinctive_error_code: {reason}"
+kept = "kept_in_same_hunk_literal"
+removed = f"replacement_code: {reason}"
"""
    literals = extract_deleted_literals(diff)

    assert "distinctive_error_code:" in literals
    assert "should_never_be_extracted_from_test_file" not in literals
    assert "comment_only_symbol" not in literals
    assert "comment_only_literal" not in literals
    assert "utf-8" not in literals
    assert "new message" not in literals
    assert "kept_in_same_hunk_literal" not in literals
    assert "a/pkg/mod.py" not in literals


def test_pr9123_style_two_hop_tests_auto_run_under_skip_tests_and_block_pass_on_failure(
    tmp_path: Path,
) -> None:
    _init_git_with_base(
        tmp_path,
        {
            "agent/graph/prompt/conversation.py": (
                "_CLAUDE_CODE_VISIBILITY_WHY = (\n"
                '    "Unlike standalone Claude Code, where output goes to a human at a terminal, "\n'
                '    "many Meiva agents report via `meiva.agents.send_message`. "\n'
                '    "When you have a delegator, reporting there completes delivery.\\n"\n'
                ")\n"
            ),
            "agent/graph/tests/prompt/test_conversation_prompt.py": (
                'def test_prompt() -> None:\n    assert "When you have a delegator" in "x"\n'
            ),
            "agent/graph/tests/test_init_context_harness.py": (
                'def test_handoff() -> None:\n    assert "meiva.agents.send_message" in "x"\n'
            ),
            "agent/graph/tests/test_unrelated.py": ("def test_other() -> None:\n    assert True\n"),
        },
    )
    (tmp_path / "agent/graph/prompt/conversation.py").write_text(
        "_CLAUDE_CODE_VISIBILITY_WHY = (\n"
        '    "Unlike standalone Claude Code, where output goes to a human at a terminal, "\n'
        '    "a Meiva worker reports to its delegator.\\n"\n'
        ")\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "agent/graph/prompt/conversation.py")
    _git(tmp_path, "commit", "-q", "-m", "reword prompt")

    selected = (
        "agent/graph/tests/prompt/test_conversation_prompt.py",
        "agent/graph/tests/test_init_context_harness.py",
        "agent/graph/tests/test_unrelated.py",
    )
    change = ChangeSet(
        root=tmp_path,
        base="origin/main",
        files=("agent/graph/prompt/conversation.py",),
    )

    ran_pytest_targets: list[tuple[str, ...]] = []

    def execute_pass(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        if "-m" in argv and "pytest" in argv:
            targets = tuple(part for part in argv if part.endswith(".py"))
            ran_pytest_targets.append(targets)
            _write_report(argv, passed=12)
            return 0, "", 1.5
        return 0, "0 errors, 0 warnings\nmeizel: ok (1 fact edges, 1 manifests)\n", 0.5

    values_ok, failed_ok = check(
        change,
        skip_tests=True,
        execute=execute_pass,
        choose=lambda _c: Selection("SELECTED", selected, ()),
        python=_PYTHON,
    )
    assert failed_ok is False
    assert ran_pytest_targets == [
        (
            "agent/graph/tests/prompt/test_conversation_prompt.py",
            "agent/graph/tests/test_init_context_harness.py",
        )
    ]
    assert values_ok["tests"] == "skipped(--skip-tests)"
    assert values_ok["literal-tests"] == "ok(2files,12passed)(2s)"
    receipt_ok = build_receipt(values_ok, "abc123", "def456", failed=failed_ok)
    assert " verdict=pass " in receipt_ok
    assert " literal-tests=ok(2files,12passed)(2s) " in receipt_ok

    def execute_fail(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        if "-m" in argv and "pytest" in argv:
            _write_report(argv, passed=11, failed=1)
            return 1, "AssertionError: meiva.agents.send_message", 1.0
        return 0, "0 errors, 0 warnings\nmeizel: ok (1 fact edges, 1 manifests)\n", 0.5

    values_fail, failed_fail = check(
        change,
        skip_tests=True,
        execute=execute_fail,
        choose=lambda _c: Selection("SELECTED", selected, ()),
        python=_PYTHON,
    )
    assert failed_fail is True
    assert values_fail["literal-tests"] == "failed(2files,1failed,0errors)(1s)"
    receipt_fail = build_receipt(values_fail, "abc123", "def456", failed=failed_fail)
    assert " verdict=fail " in receipt_fail


def test_over_cap_literal_hits_do_not_run_local_pytest_and_warn_with_preview(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hit_count = MAX_LITERAL_AUTO_TESTS + 3
    selected: list[str] = []
    for index in range(hit_count):
        rel = f"pkg/tests/test_hit_{index:02d}.py"
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('def test_x():\n    assert "legacy_protocol_token_v1" in "x"\n')
        selected.append(rel)

    diff = (
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -1 +1 @@\n"
        '-TOKEN = "legacy_protocol_token_v1"\n'
        '+TOKEN = "protocol_token_v2"\n'
    )
    pytest_called = False

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        nonlocal pytest_called
        if "-m" in argv and "pytest" in argv:
            pytest_called = True
        return 0, "0 errors, 0 warnings\nmeizel: ok (1 fact edges, 1 manifests)\n", 0.5

    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("pkg/mod.py",)),
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", tuple(selected), ()),
        read_diff=lambda _c: diff,
        python=_PYTHON,
    )
    stderr = capsys.readouterr().err
    assert pytest_called is False, "must never launch local wide pytest when hits exceed the cap"
    assert failed is False
    assert values["literal-tests"] == f"over-cap({hit_count}files>{MAX_LITERAL_AUTO_TESTS})"
    assert f"{hit_count} hit files exceed auto-run cap ({MAX_LITERAL_AUTO_TESTS})" in stderr
    assert "pkg/tests/test_hit_00.py" in stderr
    assert "verdict=pass" in build_receipt(values, "abc123", "def456", failed=failed)


def test_skip_literal_tests_flag_bypasses_scan_and_execution(tmp_path: Path) -> None:
    test_file = tmp_path / "pkg/tests/test_hit.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text('assert "distinctive_removed_token" in "x"\n')

    diff_called = False
    pytest_called = False

    def read_diff(_change: ChangeSet) -> str:
        nonlocal diff_called
        diff_called = True
        return (
            "diff --git a/pkg/mod.py b/pkg/mod.py\n"
            "--- a/pkg/mod.py\n"
            "+++ b/pkg/mod.py\n"
            "@@ -1 +1 @@\n"
            '-OLD = "distinctive_removed_token"\n'
            '+NEW = "replacement"\n'
        )

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        nonlocal pytest_called
        if "-m" in argv and "pytest" in argv:
            pytest_called = True
        return 0, "0 errors, 0 warnings\nmeizel: ok (1 fact edges, 1 manifests)\n", 0.5

    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("pkg/mod.py",)),
        skip_tests=True,
        skip_literal_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", ("pkg/tests/test_hit.py",), ()),
        read_diff=read_diff,
        python=_PYTHON,
    )
    assert failed is False
    assert diff_called is False
    assert pytest_called is False
    assert values["literal-tests"] == "skipped(--skip-literal-tests)"
    assert parse_args(["--skip-tests", "--skip-literal-tests"]).skip_literal_tests is True
    assert not any(
        "literal-tests:" in line
        for line in plan(
            ChangeSet(root=tmp_path, base="origin/main", files=("pkg/mod.py",)),
            skip_tests=True,
            skip_literal_tests=True,
            python=_PYTHON,
        )
    )


def test_zero_literal_hits_and_unchanged_hunk_incur_zero_pytest_overhead(tmp_path: Path) -> None:
    test_file = tmp_path / "pkg/tests/test_mod.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text('assert "still_present_literal_token" in "x"\n')

    unchanged_hunk_diff = (
        "diff --git a/pkg/mod.py b/pkg/mod.py\n"
        "--- a/pkg/mod.py\n"
        "+++ b/pkg/mod.py\n"
        "@@ -1 +1 @@\n"
        '-VALUE = "still_present_literal_token"\n'
        '+VALUE = "still_present_literal_token"  # tweaked\n'
    )
    pytest_called = False

    def execute(argv: Sequence[str], _root: Path) -> tuple[int, str, float]:
        nonlocal pytest_called
        if "-m" in argv and "pytest" in argv:
            pytest_called = True
        return 0, "0 errors, 0 warnings\nmeizel: ok (1 fact edges, 1 manifests)\n", 0.5

    values, failed = check(
        ChangeSet(root=tmp_path, base="origin/main", files=("pkg/mod.py",)),
        skip_tests=True,
        execute=execute,
        choose=lambda _c: Selection("SELECTED", ("pkg/tests/test_mod.py",), ()),
        read_diff=lambda _c: unchanged_hunk_diff,
        python=_PYTHON,
    )
    assert failed is False
    assert pytest_called is False
    assert values["literal-tests"] == "0hits"


def test_blind_selection_uses_bounded_package_fallback_and_excludes_conftest(
    tmp_path: Path,
) -> None:
    for rel in (
        "pkg/mod.py",
        "pkg/tests/conftest.py",
        "pkg/tests/test_one.py",
        "pkg/tests/test_two.py",
        "other/tests/test_outside.py",
    ):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('assert "pkg_specific_removed_literal" in "x"\n')

    assert is_test_path("pkg/tests/conftest.py") is True
    assert is_pytest_file("pkg/tests/conftest.py") is False
    assert is_pytest_file("pkg/tests/test_one.py") is True

    candidates = candidate_test_files(
        tmp_path,
        ("pkg/mod.py",),
        (),
        blind=("pkg/mod.py",),
        decision="BLIND",
        max_fallback_files=1,
    )
    assert candidates == ("pkg/tests/test_one.py",)

    scan = find_literal_test_hits(tmp_path, ("pkg_specific_removed_literal",), candidates)
    assert scan.hits == ("pkg/tests/test_one.py",)
    assert scan.scanned_files == 1


def test_diff_text_for_handles_missing_root_and_test_only_changes(tmp_path: Path) -> None:
    assert (
        diff_text_for(ChangeSet(root=Path("/nonexistent"), base="origin/main", files=("a.py",)))
        == ""
    )
    assert (
        diff_text_for(
            ChangeSet(root=tmp_path, base="origin/main", files=("pkg/tests/test_only.py",))
        )
        == ""
    )
