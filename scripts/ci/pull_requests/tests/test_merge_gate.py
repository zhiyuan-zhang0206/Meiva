# meiva:note
# `scripts/ci/pull_requests/gate/merge_gate.py`: the five conditions, driven over a fake `gh`.
#
# The gate decides whether this lane may merge a pull request, so its verdicts are checked
# end-to-end rather than through its helpers: the module runs as a process with `MEIVA_REAL_GH`
# pointing at a stub that answers `gh api` from a JSON world, and the assertions read the printed
# evidence and the exit status. Nothing here reaches GitHub.
#
# The failure the gate exists for is a green pull request that is not this lane's to merge, so the
# authorship cases dominate: a harness trailer on the user's own Claude Code line is refused, a
# Meiva trailer outside the grant is refused, and a trailer that exists only in the pull-request
# body is accepted (a squash merge keeps the body and drops the head commit message).

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

GATE = Path(__file__).resolve().parents[1] / "gate/merge_gate.py"
HEAD = "1" * 40
BASE = "2" * 40
MAIN = "3" * 40
FULL_NAME = "owner/repo"

STUB = """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["FAKE_GH_WORLD"], encoding="utf-8") as handle:
    world = json.load(handle)
path = sys.argv[-1]
for needle, payload in world:
    if needle in path:
        print(json.dumps(payload))
        sys.exit(0)
sys.stderr.write(f"fake gh: unhandled {path}")
sys.exit(2)
"""


def view(*, body: str, state: str = "open", head_ref: str = "meiva-7381-strict") -> dict[str, Any]:
    return {
        "number": 1,
        "title": "a pull request",
        "state": state,
        "mergeable": True,
        "mergeable_state": "clean",
        "merge_commit_sha": "4" * 40 if state == "closed" else None,
        "body": body,
        "head": {"sha": HEAD, "ref": head_ref},
        "base": {"sha": BASE, "ref": "main"},
    }


def green_runs() -> dict[str, Any]:
    return {
        "check_runs": [
            {"name": "backend shard (1/8)", "status": "completed", "conclusion": "success"},
            {"name": "frontend", "status": "completed", "conclusion": "skipped"},
            {"name": "Trunk merge queue", "status": "completed", "conclusion": "failure"},
        ]
    }


def passing_body(trailer: str = "Co-authored-by: Meiva #7381") -> str:
    return (
        f"Preflight on head {HEAD}: PREFLIGHT PASS: /trunk merge is allowed\n"
        "\nwhere_used: scripts/ci/pull_requests/gate/merge_gate.py\n"
        "\n## Behaviour\n\nthe gate refuses what it cannot read\n"
        f"\n{trailer}\n"
    )


def world(
    *,
    pull: Mapping[str, Any],
    runs: Mapping[str, Any] | None = None,
    pr_files: tuple[str, ...] = ("scripts/ci/pull_requests/gate/merge_gate.py",),
    main_files: tuple[str, ...] = (),
    commit_message: str = "squash me",
) -> list[tuple[str, object]]:
    return [
        ("pulls/1/commits", [{"commit": {"message": commit_message}}]),
        ("check-runs", green_runs() if runs is None else runs),
        (f"compare/{BASE}...{HEAD}", {"files": [{"filename": name} for name in pr_files]}),
        (f"compare/{BASE}...{MAIN}", {"files": [{"filename": name} for name in main_files]}),
        (f"commits/{HEAD}", {"sha": HEAD}),
        ("commits/main", {"sha": MAIN}),
        ("pulls/1", dict(pull)),
        ("repos/{owner}/{repo}", {"full_name": FULL_NAME}),
    ]


def run_gate(
    tmp_path: Path,
    entries: list[tuple[str, object]],
    *,
    env: Mapping[str, str] | None = None,
    lanes: tuple[str, ...] = ("7381",),
) -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "gh"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    world_file = tmp_path / "world.json"
    world_file.write_text(json.dumps(entries), encoding="utf-8")
    lane_file = tmp_path / "lane-agents.txt"
    lane_file.write_text("# the grant\n" + "".join(f"{lane}\n" for lane in lanes), encoding="utf-8")
    environment = {
        "PATH": "/usr/bin:/bin",
        "MEIVA_REAL_GH": str(stub),
        "MEIVA_LANE_FILE": str(lane_file),
        "FAKE_GH_WORLD": str(world_file),
    }
    environment.update(env or {})
    # meiva:note
    # The argv is this interpreter and a gate path this test owns, so S603 has nothing to flag.
    return subprocess.run(  # noqa: S603
        [sys.executable, str(GATE), "1"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
        check=False,
    )


def test_a_lane_pull_request_with_five_conditions_holds_is_go(tmp_path: Path) -> None:
    done = run_gate(tmp_path, world(pull=view(body=passing_body())))
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"GO: four conditions hold (head {HEAD[:12]}, main {MAIN[:12]})" in done.stdout


def test_a_trailer_in_the_body_alone_still_authors_the_lane(tmp_path: Path) -> None:
    entries = world(pull=view(body=passing_body()), commit_message="no trailer in the commit")
    done = run_gate(tmp_path, entries)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Meiva trailers: ['7381']" in done.stdout


def test_a_harness_trailer_without_a_meiva_one_is_refused(tmp_path: Path) -> None:
    body = passing_body(trailer="Co-Authored-By: Claude Sonnet 5.5")
    done = run_gate(tmp_path, world(pull=view(body=body)))
    assert done.returncode == 1
    assert "harness trailer present" in done.stdout


def test_a_handed_over_branch_may_carry_only_the_harness_trailer(tmp_path: Path) -> None:
    body = passing_body(trailer="Co-Authored-By: Claude Sonnet 5.5")
    entries = world(pull=view(body=body, head_ref="meiva-pyright-any-strict"))
    done = run_gate(tmp_path, entries)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "handed-over ladder: True" in done.stdout


def test_a_trailer_outside_the_grant_is_refused(tmp_path: Path) -> None:
    body = passing_body("Co-authored-by: Meiva #9999")
    done = run_gate(tmp_path, world(pull=view(body=body)))
    assert done.returncode == 1
    assert "trailer agent(s) ['9999'] outside the grant" in done.stdout


def test_a_red_check_run_is_refused(tmp_path: Path) -> None:
    runs = {"check_runs": [{"name": "backend", "status": "completed", "conclusion": "failure"}]}
    done = run_gate(tmp_path, world(pull=view(body=passing_body()), runs=runs))
    assert done.returncode == 1
    assert "1 check run(s) not green" in done.stdout


def test_an_unfinished_check_run_is_not_green(tmp_path: Path) -> None:
    runs = {"check_runs": [{"name": "backend", "status": "in_progress", "conclusion": None}]}
    done = run_gate(tmp_path, world(pull=view(body=passing_body()), runs=runs))
    assert done.returncode == 1
    assert "1 check run(s) not green" in done.stdout


def test_a_head_with_no_check_runs_is_refused(tmp_path: Path) -> None:
    done = run_gate(tmp_path, world(pull=view(body=passing_body()), runs={"check_runs": []}))
    assert done.returncode == 1
    assert "no check runs on this head" in done.stdout


def test_the_owed_list_and_the_generated_region_may_overlap(tmp_path: Path) -> None:
    shared = ("pyproject.toml", "scripts/codegen/pyright_tests_owing_any.txt")
    entries = world(pull=view(body=passing_body()), pr_files=shared, main_files=shared)
    done = run_gate(tmp_path, entries)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ALLOWED: shared files are only the owed list" in done.stdout


def test_any_other_shared_file_forces_a_rebase(tmp_path: Path) -> None:
    shared = ("scripts/lint/docs/lint.meiva.okf.md",)
    entries = world(pull=view(body=passing_body()), pr_files=shared, main_files=shared)
    done = run_gate(tmp_path, entries)
    assert done.returncode == 1
    assert "outside the generated-region pair" in done.stdout


def test_a_truncated_compare_is_refused(tmp_path: Path) -> None:
    moved = tuple(f"f{index}.py" for index in range(300))
    done = run_gate(tmp_path, world(pull=view(body=passing_body()), main_files=moved))
    assert done.returncode == 1
    assert "compare truncated at 300 files" in done.stdout


def test_a_preflight_pinned_to_another_head_is_refused(tmp_path: Path) -> None:
    body = passing_body().replace(HEAD, "9" * 40, 1)
    done = run_gate(tmp_path, world(pull=view(body=body)))
    assert done.returncode == 1
    assert "body pins 999999999999..." in done.stdout


def test_a_body_without_the_lane_conventions_is_refused(tmp_path: Path) -> None:
    body = (
        f"Preflight on head {HEAD}: PREFLIGHT PASS: /trunk merge is allowed\n"
        "\nCo-authored-by: Meiva #7381\n"
    )
    done = run_gate(tmp_path, world(pull=view(body=body)))
    assert done.returncode == 1
    assert "body has no where_used" in done.stdout
    assert "body has no behaviour section" in done.stdout


def test_a_reviewer_merge_waives_the_body_conditions_but_not_the_checks(tmp_path: Path) -> None:
    runs = {"check_runs": [{"name": "backend", "status": "completed", "conclusion": "failure"}]}
    body = "no preflight, no conventions, no trailer"
    entries = world(pull=view(body=body), runs=runs)
    done = run_gate(tmp_path, entries, env={"MEIVA_MERGE_REVIEWER": "1"})
    assert done.returncode == 1
    assert "1 check run(s) not green" in done.stdout
    assert "harness trailer present" not in done.stdout
    assert "body has no where_used" not in done.stdout


def test_a_reviewer_merge_with_green_checks_goes(tmp_path: Path) -> None:
    body = "no preflight, no conventions, no trailer"
    done = run_gate(tmp_path, world(pull=view(body=body)), env={"MEIVA_MERGE_REVIEWER": "1"})
    assert done.returncode == 0, done.stdout + done.stderr
    assert "GO (reviewer merge)" in done.stdout


def test_a_merged_pull_request_passes_without_reading_the_conditions(tmp_path: Path) -> None:
    body = "no preflight, no conventions, no trailer"
    done = run_gate(tmp_path, world(pull=view(body=body, state="closed")))
    assert done.returncode == 0, done.stdout + done.stderr
    assert "already merged as" in done.stdout


def test_a_closed_unmerged_pull_request_is_refused(tmp_path: Path) -> None:
    pull = view(body=passing_body(), state="closed")
    pull["merge_commit_sha"] = None
    done = run_gate(tmp_path, world(pull=pull))
    assert done.returncode == 1
    assert "PR is closed and not merged" in done.stdout


def test_a_missing_grant_refuses_instead_of_narrowing_it(tmp_path: Path) -> None:
    entries = world(pull=view(body=passing_body()))
    absent = str(tmp_path / "no-such-grant.txt")
    done = run_gate(tmp_path, entries, env={"MEIVA_LANE_FILE": absent})
    assert done.returncode == 1
    assert "lane list missing at" in done.stdout


def test_a_grant_that_names_nobody_refuses(tmp_path: Path) -> None:
    done = run_gate(tmp_path, world(pull=view(body=passing_body())), lanes=())
    assert done.returncode == 1
    assert "names no lane" in done.stdout


def test_the_grant_comes_from_the_environment_when_it_is_set(tmp_path: Path) -> None:
    body = passing_body("Co-authored-by: Meiva #4242")
    entries = world(pull=view(body=body))
    done = run_gate(tmp_path, entries, env={"MEIVA_LANE_AGENTS": "7381,4242"})
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("bad", ["not a number", "https://example.test/nope"])
def test_an_argument_without_a_pull_request_number_is_refused(tmp_path: Path, bad: str) -> None:
    # meiva:note
    # The argument is the case under test; the interpreter and the path are this test's own.
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(GATE), bad],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 1
    assert "NO-GO: no PR number in arguments" in done.stdout


def test_the_self_test_runs_offline(tmp_path: Path) -> None:
    # meiva:note
    # No gh on PATH and no world file: the self-check must not need either.
    done = subprocess.run(  # noqa: S603
        [sys.executable, str(GATE), "--selftest"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "8/8 cases as expected" in done.stdout
