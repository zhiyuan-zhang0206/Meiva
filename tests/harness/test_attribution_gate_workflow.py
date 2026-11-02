# meiva:note
# Runs the checked-in shell of the attribution-gate workflow against a mock GitHub API. Every case
# asserts the comment state a run leaves behind, because this workflow may never become a merge
# blocker. The mock keeps comments in a JSON file and evaluates the workflow's own --jq programs
# with the real jq in raw-output mode, as `gh api --jq` prints strings.
# meiva:link .github/repository-automation.meiva.okf.md

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/attribution-gate.yml"
TEMPLATE = ROOT / ".github/PULL_REQUEST_TEMPLATE.md"
STEP = "Reconcile the attribution comment"
MARKER = "<!-- meiva-attribution-gate -->"
REPO = "owner/repository"
BOT = "github-actions[bot]"


# meiva:note
# The first jq this machine has, or None. The mock evaluates the workflow's --jq with it, so the
# checked-in jq program is what runs rather than a Python re-implementation of it. The probed
# executable is this machine's own jq, not untrusted input.
def _resolve_jq(candidates: tuple[str | None, ...] | None = None) -> str | None:
    if candidates is None:
        candidates = (
            os.environ.get("JQ"),
            "/usr/bin/jq",
            "/usr/local/bin/jq",
            "/opt/homebrew/bin/jq",
        )
    for candidate in candidates:
        if candidate is None or not os.access(candidate, os.X_OK):
            continue
        probe = subprocess.run(  # noqa: S603
            [candidate, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
        if probe.returncode == 0:
            return candidate
    return None


JQ = _resolve_jq()

MOCK = r"""#!__PYTHON__
import json, os, subprocess, sys

state_path = os.environ["GH_STATE"]
with open(state_path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
with open(os.environ["GH_CALL_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if os.environ.get("GH_API_RESULT") == "failure":
    print("gh: mock api failure", file=sys.stderr)
    sys.exit(1)

method = "GET"
program = None
fields = {}
endpoint = None
index = 0
while index < len(args):
    argument = args[index]
    if argument == "--method":
        method = args[index + 1]
        index += 2
    elif argument == "--jq":
        program = args[index + 1]
        index += 2
    elif argument in ("-f", "-F"):
        key, _, value = args[index + 1].partition("=")
        fields[key] = value
        index += 2
    elif argument == "--paginate":
        index += 1
    else:
        endpoint = argument
        index += 1


def emit(payload):
    result = subprocess.run(
        [os.environ["JQ"], "-r", program], input=json.dumps(payload),
        capture_output=True, text=True, check=True,
    )
    sys.stdout.write(result.stdout)


def save():
    with open(state_path, "w") as handle:
        json.dump(state, handle)


def payload(comment):
    return {"id": comment["id"], "body": comment["body"], "user": {"login": comment["author"]}}


path = (endpoint or "").split("?")[0]
parts = path.split("/")
if method == "GET" and len(parts) == 5 and parts[0] == "repos" and parts[3] == "pulls":
    emit({"body": state["body"]})
elif method == "GET" and parts[-1] == "comments":
    emit([payload(comment) for comment in state["comments"]])
elif method == "POST" and parts[-1] == "comments":
    comment = {"id": state["next_id"], "body": fields["body"], "author": state["bot"]}
    state["comments"].append(comment)
    state["next_id"] += 1
    save()
    print(json.dumps(comment))
elif method == "DELETE" and "issues" in parts and "comments" in parts:
    comment_id = int(parts[-1])
    state["comments"] = [c for c in state["comments"] if c["id"] != comment_id]
    save()
else:
    print("unexpected request: " + method + " " + path, file=sys.stderr)
    sys.exit(2)
"""


def script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["attribution"]
    return next(step["run"] for step in job["steps"] if step["name"] == STEP)


class Run(NamedTuple):
    result: subprocess.CompletedProcess[str]
    comments: list[dict[str, object]]
    calls: list[list[str]]


def bot_comment(text: str) -> tuple[str, str]:
    return (BOT, text)


# meiva:note
# Runs the workflow's own shell with the isolated mock `gh` first on PATH: the executable is the
# checked-in script and the environment is built here, so neither is untrusted input.
def run_gate(
    tmp_path: Path,
    *,
    body: str | None,
    head_ref: str = "meiva-7317-attribution-gate",
    comments: tuple[tuple[str, str], ...] = (),
    api_result: str = "ok",
) -> Run:
    assert JQ is not None, "jq is required: the mock evaluates the workflow's --jq with it"
    mock = tmp_path / "gh"
    mock.write_text(MOCK.replace("__PYTHON__", sys.executable))
    mock.chmod(0o700)
    state_path = tmp_path / "state.json"
    state = {
        "body": body,
        "bot": BOT,
        "comments": [
            {"id": 1000 + offset, "author": author, "body": text}
            for offset, (author, text) in enumerate(comments)
        ],
        "next_id": 2000,
    }
    state_path.write_text(json.dumps(state))
    call_log = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.defpath}",
        "JQ": str(JQ),
        "GH_TOKEN": "unused",
        "GITHUB_REPOSITORY": REPO,
        "PR_NUMBER": "42",
        "HEAD_REF": head_ref,
        "GH_STATE": str(state_path),
        "GH_CALL_LOG": str(call_log),
        "GH_API_RESULT": api_result,
    }
    result = subprocess.run(  # noqa: S603
        ["bash", "-c", script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    final = json.loads(state_path.read_text())
    calls = (
        [json.loads(line) for line in call_log.read_text().splitlines()]
        if call_log.exists()
        else []
    )
    return Run(result, final["comments"], calls)


def posted(run: Run) -> list[list[str]]:
    return [call for call in run.calls if "POST" in call]


def deleted(run: Run) -> list[list[str]]:
    return [call for call in run.calls if "DELETE" in call]


def test_a_meiva_branch_without_attribution_gets_one_comment(tmp_path: Path) -> None:
    run = run_gate(tmp_path, body="## What and why\n\nSomething changed.\n")
    assert run.result.returncode == 0, run.result.stderr
    assert len(run.comments) == 1
    assert MARKER in str(run.comments[0]["body"])
    assert "Co-authored-by: Meiva #<id>" in posted(run)[0][-1]


def test_an_agent_id_gets_no_comment(tmp_path: Path) -> None:
    run = run_gate(tmp_path, body="What changed.\n\nCo-authored-by: Meiva #7317\n")
    assert run.result.returncode == 0, run.result.stderr
    assert run.comments == []
    assert not posted(run)


# meiva:note
# The template's own `<id>` placeholder is not attribution: a comment that accepted it would
# announce itself satisfied for every pull request filed from the template.
def test_the_agent_id_needs_digits(tmp_path: Path) -> None:
    run = run_gate(tmp_path, body="Co-authored-by: Meiva #<id>\n")
    assert len(run.comments) == 1


def test_an_added_agent_id_deletes_the_comment(tmp_path: Path) -> None:
    run = run_gate(
        tmp_path,
        body="Fixed.\n\nCo-authored-by: Meiva #7317\n",
        comments=(bot_comment(f"{MARKER}\nThis pull request names no agent.\n"),),
    )
    assert run.result.returncode == 0, run.result.stderr
    assert run.comments == []
    assert len(deleted(run)) == 1


def test_the_human_exit_deletes_the_comment(tmp_path: Path) -> None:
    run = run_gate(
        tmp_path, body="Owner's own work.\n\nMeiva: none\n", comments=(bot_comment(f"{MARKER}\n"),)
    )
    assert run.result.returncode == 0, run.result.stderr
    assert run.comments == []


def test_the_human_exit_tolerates_padding_and_case(tmp_path: Path) -> None:
    run = run_gate(
        tmp_path,
        body="Owner's own work.\n\n   MEIVA:  None   \n",
        comments=(bot_comment(f"{MARKER}\n"),),
    )
    assert run.comments == []


# meiva:note
# Only a line of its own is a statement by the author; a description that merely mentions the
# exit keeps the comment.
def test_a_description_that_merely_mentions_the_exit_still_gets_the_comment(
    tmp_path: Path,
) -> None:
    run = run_gate(
        tmp_path,
        body="The owner wrote this, so it uses the `Meiva: none` line as the template says.\n",
    )
    assert len(run.comments) == 1


def test_a_branch_outside_the_convention_gets_no_comment(tmp_path: Path) -> None:
    run = run_gate(tmp_path, body="No attribution here.\n", head_ref="fix-the-gate")
    assert run.result.returncode == 0, run.result.stderr
    assert run.comments == []


def test_a_branch_outside_the_convention_removes_a_leftover_comment(tmp_path: Path) -> None:
    run = run_gate(
        tmp_path,
        body="No attribution here.\n",
        head_ref="fix-the-gate",
        comments=(bot_comment(f"{MARKER}\n"),),
    )
    assert run.comments == []


def test_two_comments_become_one_and_keep_the_first(tmp_path: Path) -> None:
    run = run_gate(
        tmp_path,
        body="No attribution here.\n",
        comments=(bot_comment(f"{MARKER}\nfirst\n"), bot_comment(f"{MARKER}\nsecond\n")),
    )
    assert [comment["id"] for comment in run.comments] == [1000]


def test_an_unrelated_comment_is_left_alone(tmp_path: Path) -> None:
    run = run_gate(
        tmp_path,
        body="Fixed.\n\nCo-authored-by: Meiva #7317\n",
        comments=(("a-person", "Unrelated review note.\n"),),
    )
    assert [comment["body"] for comment in run.comments] == ["Unrelated review note.\n"]


# meiva:note
# `contains` alone would also match a person quoting the reminder; only the comment this workflow
# posted may be deleted.
def test_a_comment_quoting_the_marker_is_left_alone(tmp_path: Path) -> None:
    run = run_gate(
        tmp_path,
        body="Fixed.\n\nCo-authored-by: Meiva #7317\n",
        comments=(("a-person", f"{MARKER}\nWhat does this mean?\n"),),
    )
    assert [comment["body"] for comment in run.comments] == [f"{MARKER}\nWhat does this mean?\n"]


def test_a_second_run_keeps_the_single_comment(tmp_path: Path) -> None:
    run = run_gate(
        tmp_path,
        body="No attribution here.\n",
        comments=(bot_comment(f"{MARKER}\nThis pull request names no agent.\n"),),
    )
    assert [comment["id"] for comment in run.comments] == [1000]
    assert not posted(run)
    assert not deleted(run)


# meiva:note
# The committed template is run as a body: a placeholder or a mention inside an HTML comment that
# satisfied this gate would silence every pull request filed from it.
def test_the_committed_template_does_not_satisfy_the_gate(tmp_path: Path) -> None:
    run = run_gate(tmp_path, body=TEMPLATE.read_text())
    assert len(run.comments) == 1


# meiva:note
# A case-insensitive filesystem hides that `PULL_REQUEST_TEMPLATE.md` and
# `pull_request_template.md` are one file here; git records the name GitHub must find. The query
# is a fixed `git ls-files` in this repository, not untrusted input.
def test_the_template_is_committed_at_one_path() -> None:
    tracked = subprocess.run(  # noqa: S603
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            ".github/PULL_REQUEST_TEMPLATE.md",
            ".github/pull_request_template.md",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert tracked == [".github/PULL_REQUEST_TEMPLATE.md"]


def test_the_template_requires_the_attribution_line() -> None:
    assert "Co-authored-by: Meiva #<id>" in TEMPLATE.read_text()


# meiva:note
# A shared account cannot prove who wrote a branch, so a failed call here may not become a red
# check: every step of the job must continue on error.
def test_a_failed_api_call_cannot_fail_the_job(tmp_path: Path) -> None:
    run = run_gate(tmp_path, body="No attribution here.\n", api_result="failure")
    assert run.result.returncode != 0
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["attribution"]["steps"]
    assert steps, "the job has no step to run"
    assert all(step.get("continue-on-error") is True for step in steps)


# meiva:note
# `edited` is what lets the comment answer a description that gained its line.
def test_the_trigger_closes_the_loop_and_targets_pull_requests() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    trigger = workflow["on" if "on" in workflow else True]
    assert set(trigger) == {"pull_request"}
    assert {"opened", "edited"}.issubset(trigger["pull_request"]["types"])
    assert workflow["permissions"]["pull-requests"] == "write"


# meiva:note
# No `uses:` step to pin, and nothing that could report a conclusion of its own.
def test_the_job_runs_no_action_and_only_comments() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["attribution"]["steps"]
    assert not [step for step in steps if "uses" in step]
    assert "set -euo pipefail" in steps[0]["run"]
