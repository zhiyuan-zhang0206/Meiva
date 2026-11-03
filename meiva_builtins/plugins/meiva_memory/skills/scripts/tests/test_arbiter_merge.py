# meiva:note
# `arbiter_merge.py` is a script run as its own process: it lists the pool's open PRs
# through `gh` and merges the ones the pool gate vouched for. Tests drive it with a
# fake `gh` on PATH so the merge/no-merge decision is observable, not inferred.
import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "arbiter_merge.py"

FAKE_GH = """#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
log = os.environ["FAKE_GH_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(" ".join(argv) + "\\n")

state = json.loads(os.environ["FAKE_GH_STATE"])
if argv[:2] == ["pr", "list"]:
    print("\\n".join(str(n) for n in state["open"]))
elif argv[:2] == ["pr", "view"] and "--json" in argv:
    number, field = argv[2], argv[argv.index("--json") + 1]
    pr = state["prs"][number]
    if field == "statusCheckRollup":
        print(json.dumps({"statusCheckRollup": pr.get("checks", [])}))
    elif field == "body":
        print(json.dumps({"body": pr.get("body", "")}))
    else:
        sys.exit(3)
elif argv[:2] == ["pr", "edit"]:
    number = argv[2]
    body = sys.stdin.read()
    with open(os.path.join(os.environ["FAKE_GH_STATE_DIR"], number + ".body"), "w", encoding="utf-8") as fh:
        fh.write(body)
elif argv[:2] == ["pr", "merge"]:
    pass
else:
    sys.exit(4)
"""


def _repo(tmp_path: Path) -> Path:
    pool = tmp_path / "memory"
    pool.mkdir()
    git = ["git", "-C", str(pool)]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "remote", "add", "origin", "git@github.com:owner/pool.git"], check=True)
    (pool / "note.md").write_text("---\ntitle: t\ndescription: d\n---\n", encoding="utf-8")
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "init"], check=True
    )
    subprocess.run([*git, "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
    return pool


def _run(
    tmp_path: Path, state: dict, *args: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    fake_meiva = bin_dir / "meiva"
    fake_meiva.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_meiva.chmod(0o755)
    log = tmp_path / "gh.log"
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("MEIVA_")}
    env.update(
        {
            "MEIVA_HOME": str(tmp_path),
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_STATE": json.dumps(state),
            "FAKE_GH_STATE_DIR": str(state_dir),
        }
    )
    res = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return res, calls


def _checks(state: str) -> list[dict[str, str]]:
    return [{"name": "validate", "conclusion": state, "status": "COMPLETED"}]


def test_a_pr_whose_validate_check_failed_is_not_merged(tmp_path: Path) -> None:
    _repo(tmp_path)
    res, calls = _run(
        tmp_path,
        {"open": [11], "prs": {"11": {"checks": _checks("FAILURE"), "body": "red"}}},
    )
    assert not any(c.startswith("pr merge") for c in calls), calls
    assert "validate FAILURE" in res.stdout + res.stderr


def test_a_pr_with_no_validate_check_is_not_merged(tmp_path: Path) -> None:
    _repo(tmp_path)
    res, calls = _run(tmp_path, {"open": [12], "prs": {"12": {"checks": [], "body": ""}}})
    assert not any(c.startswith("pr merge") for c in calls), calls
    assert "validate ABSENT" in res.stdout + res.stderr


def test_a_green_pr_is_merged_without_deleting_the_branch_before_the_snapshot(
    tmp_path: Path,
) -> None:
    _repo(tmp_path)
    res, calls = _run(
        tmp_path,
        {"open": [13], "prs": {"13": {"checks": _checks("SUCCESS"), "body": "green"}}},
        "--apply",
    )
    merge = [c for c in calls if c.startswith("pr merge")]
    assert merge, calls
    assert "--delete-branch" not in merge[0]
    edit = [c for c in calls if c.startswith("pr edit")]
    assert edit, "the participation snapshot must be written before merging"
    assert calls.index(edit[0]) < calls.index(merge[0])
    body = (tmp_path / "state" / "13.body").read_text(encoding="utf-8")
    assert "## Collector snapshot (taken before this merge)" in body
    assert "green" in body, "the author's own body must survive"
    assert res.returncode == 0


def test_an_unreadable_check_list_is_not_a_pass(tmp_path: Path) -> None:
    _repo(tmp_path)
    res, calls = _run(tmp_path, {"open": [14], "prs": {}})
    assert not any(c.startswith("pr merge") for c in calls), calls
    assert res.returncode != 0


def test_it_is_dormant_by_default_and_only_reports(tmp_path: Path) -> None:
    _repo(tmp_path)
    res, calls = _run(
        tmp_path,
        {"open": [15], "prs": {"15": {"checks": _checks("SUCCESS"), "body": "green"}}},
    )
    assert not any(c.startswith("pr merge") for c in calls), calls
    assert not any(c.startswith("pr edit") for c in calls), calls
    assert "would merge PR #15" in res.stdout
    assert res.returncode == 0
