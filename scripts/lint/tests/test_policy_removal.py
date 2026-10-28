# meiva:note
# The gate's three answers: a removal without a line fails, with one passes, an addition asks
# nothing. Each test builds a throwaway repository and runs the command the workflow runs.


from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.lint import policy_removal

SEPARATOR = " \u2014 "
POLICY = (
    "[external]\n"
    '"app/runner.py" = "executes the schedule script the user wrote"\n'
    "\n[loads]\n"
    "\n[default-arguments]\n"
)
ENTRY = '"app/runner.py" = "executes the schedule script the user wrote"\n'
LINE = (
    f"Policy-Removal: external.app/runner.py{SEPARATOR}the schedule script runs as a module now"
    f"{SEPARATOR}app/runner.py loads it through [loads]"
)


# meiva:note
# A throwaway repository whose one commit holds the policy the change is compared against.
def _repository(tmp_path: Path, policy: str = POLICY) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main", "-q", str(root)], check=True)  # noqa: S603
    (root / "meizel.policy.toml").write_text(policy, encoding="utf-8")
    _commit(root, "start")
    return root


# meiva:note
# A commit with the given message: the record a run without a pull request reads.
def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)  # noqa: S603
    subprocess.run(  # noqa: S603
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )


def _run(root: Path, *args: str) -> int:
    return policy_removal.main(["--repo", str(root), "--against", "HEAD", *args])


def _removed(root: Path) -> None:
    path = root / "meizel.policy.toml"
    path.write_text(path.read_text(encoding="utf-8").replace(ENTRY, ""), encoding="utf-8")


def _event(tmp_path: Path, body: str, ref: str = "feature") -> Path:
    path = tmp_path / "event.json"
    path.write_text(
        json.dumps(
            {"pull_request": {"title": "Narrow the policy", "body": body, "head": {"ref": ref}}}
        ),
        encoding="utf-8",
    )
    return path


def test_an_unchanged_policy_reports_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    summary = tmp_path / "summary.md"
    assert _run(root, "--on-removal=fail", "--summary", str(summary)) == 0
    out = capsys.readouterr().out
    assert "0 removed, 0 added" in out
    written = summary.read_text(encoding="utf-8")
    assert "Allowance policy: 0 removed, 0 added" in written
    assert written.strip() in out


def test_an_added_declaration_asks_for_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    path = root / "meizel.policy.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            ENTRY, ENTRY + '"app/other.py" = "runs code the operator names"\n'
        ),
        encoding="utf-8",
    )
    assert _run(root, "--on-removal=fail") == 0
    assert "0 removed, 1 added" in capsys.readouterr().out


def test_a_removal_without_a_line_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _repository(tmp_path)
    _removed(root)
    summary = tmp_path / "summary.md"
    assert _run(root, "--on-removal=fail", "--summary", str(summary)) == 1
    captured = capsys.readouterr()
    assert "1 removed, 0 added" in captured.out
    assert "have no `Policy-Removal` line" in captured.err
    text = summary.read_text(encoding="utf-8")
    assert "**1 removed declaration(s) unanswered:**" in text
    assert f"Policy-Removal: external.app/runner.py{SEPARATOR}" in text


def test_a_removal_answered_in_a_file_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    _removed(root)
    record = tmp_path / "record.txt"
    record.write_text(LINE + "\n", encoding="utf-8")
    assert _run(root, "--on-removal=fail", "--record", str(record)) == 0
    assert "every removed declaration is answered" in capsys.readouterr().out


def test_the_pull_request_body_is_the_record(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _removed(root)
    event = _event(tmp_path, f"## Why\n\n{LINE}\n")
    assert _run(root, "--on-removal=fail", "--event", str(event)) == 0


def test_a_line_outside_the_record_does_not_answer(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _removed(root)
    event = _event(tmp_path, "the policy lost an entry, no line here\n")
    assert _run(root, "--on-removal=fail", "--event", str(event)) == 1


def test_a_trunk_merge_branch_reads_the_commit_that_lands(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _removed(root)
    _commit(root, f"Narrow the policy\n\n{LINE}\n")
    event = _event(tmp_path, "the queue's own body, which carries no line", ref="trunk-merge/queue")
    assert (
        policy_removal.main(
            ["--repo", str(root), "--against", "HEAD~1", "--on-removal=fail", "--event", str(event)]
        )
        == 0
    )


def test_a_local_run_reads_the_branch_commits(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _removed(root)
    _commit(root, f"Narrow the policy\n\n{LINE}\n")
    assert (
        policy_removal.main(["--repo", str(root), "--against", "HEAD~1", "--on-removal=fail"]) == 0
    )


def test_a_line_that_does_not_parse_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    record = tmp_path / "record.txt"
    record.write_text(f"Policy-Removal: external.app/runner.py{SEPARATOR}gone\n", encoding="utf-8")
    assert _run(root, "--on-removal=fail", "--record", str(record)) == 1
    assert "is not a declaration" in capsys.readouterr().err


def test_warn_mode_reports_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    _removed(root)
    assert _run(root, "--on-removal=warn") == 0
    assert "have no `Policy-Removal` line" in capsys.readouterr().err


def test_a_policy_that_does_not_parse_stops_the_gate(tmp_path: Path) -> None:
    root = _repository(tmp_path, policy="[external\n")
    assert _run(root, "--on-removal=fail") == 2


def test_the_workflow_runs_the_gate_where_it_fails() -> None:
    workflow = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "scripts/lint/policy_removal.py" in workflow
    assert "--on-removal=fail" in workflow
    assert '--summary "$GITHUB_STEP_SUMMARY"' in workflow


def test_a_body_that_only_quotes_the_format_fails_a_real_removal(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _removed(root)
    event = _event(
        tmp_path,
        "Policy-Removal: <table>[.<entry>] — <why it is no longer needed>"
        " — <what replaces it, or the gap it leaves>",
    )
    assert _run(root, "--on-removal=fail", "--event", str(event)) == 1


def test_a_line_missing_its_prose_fails_and_names_the_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    record = tmp_path / "record.txt"
    record.write_text(f"Policy-Removal: external.app/runner.py{SEPARATOR}the module moved away\n")
    assert _run(root, "--on-removal=fail", "--record", str(record)) == 1
    err = capsys.readouterr().err
    assert "<what replaces it, or the gap it leaves>" in err
    assert "missing" in err
