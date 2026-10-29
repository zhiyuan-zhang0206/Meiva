# meiva:note
# The gate's three answers: a removal without a line fails, with one passes, an addition asks
# nothing. Each test builds a throwaway repository and runs the command the workflow runs.


from __future__ import annotations

import json
import re
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


def _no_reader(number: int) -> str | None:
    raise AssertionError(f"pull request {number} was read outside a queue case")


def _unreadable(_number: int) -> str | None:
    return None


# meiva:note
# The command the workflow runs. The default reader fails the test if it is reached, so no case that
# is not about the queue can touch `gh`; a queue case passes the answer it wants read.
def _run(root: Path, *args: str, read_member: policy_removal.MemberReader = _no_reader) -> int:
    return policy_removal.main(
        ["--repo", str(root), "--against", "HEAD", *args], read_member=read_member
    )


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


# meiva:note
# A Trunk batch body, as the queue writes it: the base commit and the queue page are links too, and
# the pull requests the batch tests sit under their own header — the shape the reader must not
# mistake for prose.
def _batch_body(*numbers: int) -> str:
    links = ", ".join(f"[{number}](https://www.github.com/o/r/pull/{number})" for number in numbers)
    return (
        "This pull request was created and is being managed by "
        "[Trunk Merge](https://docs.trunk.io/merge-queue).\n\n"
        "This pull request is based on the [main branch](https://github.com/o/r/tree/main) at "
        "[SHA 77c0faf4](https://github.com/o/r/commit/77c0faf4).\n\n"
        "See more details [here](https://app.trunk.io/o/merge-queue/abc/7603).\n\n"
        "## Pull Requests Being Tested\n\n\n"
        f"This pull request is testing the changes from pull requests {links}.\n\n\n"
        "## Batch Bisection\n\nThis pull request is in a [batch "
        "bisection](https://docs.trunk.io/merge-queue/batching#bisection).\n"
    )


def _queue(tmp_path: Path, *numbers: int) -> Path:
    return _event(tmp_path, _batch_body(*numbers), ref="trunk-merge/pr-7603/abc-bisection")


def test_a_queue_batch_reads_the_pull_requests_it_tests(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _removed(root)
    event = _queue(tmp_path, 7603)
    answers = {7603: f"Narrow the policy\n\n{LINE}\n"}
    assert _run(root, "--on-removal=fail", "--event", str(event), read_member=answers.get) == 0


def test_a_queue_batch_whose_pull_request_has_no_line_fails(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _removed(root)
    event = _queue(tmp_path, 7603)
    answers = {7603: "Narrow the policy\n\nthe queue's own body, carrying no line\n"}
    assert _run(root, "--on-removal=fail", "--event", str(event), read_member=answers.get) == 1


def test_a_queue_batch_answers_from_any_of_its_pull_requests(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    _removed(root)
    event = _queue(tmp_path, 7603, 7618, 7621)
    answers = {
        7603: "one\n\nnothing here\n",
        7618: f"two\n\n{LINE}\n",
        7621: "three\n\nnothing here either\n",
    }
    assert _run(root, "--on-removal=fail", "--event", str(event), read_member=answers.get) == 0


def test_a_queue_batch_that_cannot_read_its_pull_requests_passes_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    _removed(root)
    event = _queue(tmp_path, 7603)
    summary = tmp_path / "summary.md"
    assert (
        _run(
            root,
            "--on-removal=fail",
            "--event",
            str(event),
            "--summary",
            str(summary),
            read_member=_unreadable,
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "1 removed, 0 added" in out
    assert "not judged" in out
    written = summary.read_text(encoding="utf-8")
    assert "**Not judged here:**" in written
    assert "unanswered" not in written


def test_a_queue_batch_with_no_member_section_is_also_unjudged(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    _removed(root)
    event = _event(tmp_path, "the queue's own body, which carries no header", ref="trunk-merge/q")
    assert _run(root, "--on-removal=fail", "--event", str(event), read_member=_no_reader) == 0
    assert "not judged" in capsys.readouterr().out


def test_a_queue_batch_that_changed_nothing_says_nothing_about_judging(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repository(tmp_path)
    event = _queue(tmp_path, 7603)
    assert _run(root, "--on-removal=fail", "--event", str(event), read_member=_unreadable) == 0
    assert "not judged" not in capsys.readouterr().out


def test_a_batch_of_two_names_both_and_ignores_the_label_beside_them() -> None:
    body = (
        "## Pull Requests Being Tested\n\n\n"
        "This pull request is testing a batch with the changes from pull requests "
        "[7620](https://www.github.com/o/r/pull/7620) and "
        "[7600](https://www.github.com/o/r/pull/7600) - "
        "[batching documentation](https://docs.trunk.io/merge-queue/batching).\n"
    )
    assert policy_removal.member_numbers(body) == [7620, 7600]


def test_the_member_numbers_are_the_links_under_the_queue_header() -> None:
    assert policy_removal.member_numbers(_batch_body(7603)) == [7603]
    assert policy_removal.member_numbers(_batch_body(7603, 7618, 7621)) == [7603, 7618, 7621]


def test_a_body_without_the_queue_header_names_no_members() -> None:
    assert policy_removal.member_numbers("See the change in [7603](https://x/pull/7603).") == []


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


# meiva:note
# One step of the workflow, so a claim about wiring is about the step that runs the gate.
def _step(workflow: str, name: str) -> str:
    steps = workflow.split("\n      - ")
    return next(step for step in steps if step.startswith(f"name: {name}"))


# meiva:note
# One job of the workflow: its text up to the next top-level key.
def _job(workflow: str, name: str) -> str:
    body = workflow.split(f"\n  {name}:\n", 1)[1]
    return re.split(r"\n  [a-z][\w-]*:\n", body, maxsplit=1)[0]


def test_the_workflow_runs_the_gate_where_it_fails() -> None:
    workflow = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    step = _step(workflow, "Allowance policy removals need a declaration")
    assert "scripts/lint/policy_removal.py" in step
    assert "--on-removal=fail" in step
    assert '--summary "$GITHUB_STEP_SUMMARY"' in step


def test_the_workflow_lets_the_queue_case_read_its_pull_requests() -> None:
    workflow = (Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    step = _step(workflow, "Allowance policy removals need a declaration")
    assert "GH_TOKEN: ${{ github.token }}" in step
    assert "pull-requests: read" in _job(workflow, "backend-structure")


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
