from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.host_ops.release import base as release_base


def _record(
    *,
    version: str = "1.2.3",
    sha: str = "gated",
    gate_run: str = "https://github.com/o/r/actions/runs/1",
    passed: bool = True,
    hotfix: bool = False,
    followup_gate_run: str | None = None,
) -> release_base.ReleaseRecord:
    return release_base.ReleaseRecord(
        version=version,
        sha=sha,
        gate_run=gate_run,
        passed=passed,
        hotfix=hotfix,
        followup_gate_run=followup_gate_run,
    )


def _ancestor_of(edges: dict[str, set[str]]) -> Callable[[str, str], bool]:

    def is_ancestor(base: str, target: str) -> bool:
        seen: set[str] = set()
        stack = [target]
        while stack:
            node = stack.pop()
            if node == base:
                return True
            if node in seen:
                continue
            seen.add(node)
            stack.extend(edges.get(node, set()))
        return False

    return is_ancestor


def test_target_descending_from_a_gated_release_is_allowed() -> None:
    verdict = release_base.verify_release_base(
        "deployed",
        [_record()],
        _ancestor_of({"deployed": {"gated"}}),
    )

    assert verdict.ok
    assert verdict.reason == "descends-from-release/1.2.3"
    assert verdict.base is not None and verdict.base.sha == "gated"


def test_no_provenance_at_all_refuses() -> None:
    verdict = release_base.verify_release_base("deployed", [], _ancestor_of({}))

    assert not verdict.ok
    assert verdict.reason == "no-provenance"


# meiva:note
# The bootstrap exists because the record can only come from the run that certifies it: without it the first release is locked out for good (2026-10-29, the first real gate run). It stays a one-shot — allowed only by the caller, and never by an empty directory.
def test_an_empty_release_history_may_bootstrap_the_first_release() -> None:
    verdict = release_base.verify_release_base(
        "deployed",
        [],
        _ancestor_of({}),
        bootstrap_allowed=True,
    )

    assert verdict.ok
    assert verdict.reason == "bootstrap-no-release-history"
    assert verdict.base is None


def test_the_bootstrap_needs_the_caller_to_allow_it() -> None:
    verdict = release_base.verify_release_base(
        "deployed",
        [],
        _ancestor_of({}),
        bootstrap_allowed=False,
    )

    assert not verdict.ok
    assert verdict.reason == "no-provenance"


def test_a_record_that_is_not_an_ancestor_still_refuses_beside_the_bootstrap() -> None:
    verdict = release_base.verify_release_base(
        "side-branch",
        [_record()],
        _ancestor_of({"side-branch": {"base"}}),
        bootstrap_allowed=True,
    )

    assert not verdict.ok
    assert verdict.reason == "no-gated-ancestor"


def test_a_commit_that_does_not_descend_from_any_gated_tree_refuses() -> None:
    verdict = release_base.verify_release_base(
        "side-branch",
        [_record()],
        _ancestor_of({"side-branch": {"base"}}),
    )

    assert not verdict.ok
    assert verdict.reason == "no-gated-ancestor"


def test_a_hotfix_without_a_follow_up_gate_run_is_not_a_usable_base() -> None:
    hotfix = _record(sha="hot", hotfix=True, followup_gate_run=None)
    verdict = release_base.verify_release_base(
        "deployed",
        [hotfix],
        _ancestor_of({"deployed": {"hot"}}),
    )

    assert not verdict.ok
    assert verdict.reason == "hotfix-not-followed-up"


def test_a_hotfix_with_its_follow_up_gate_run_is_a_usable_base() -> None:
    hotfix = _record(sha="hot", hotfix=True, followup_gate_run="https://example/run/2")
    verdict = release_base.verify_release_base(
        "deployed",
        [hotfix],
        _ancestor_of({"deployed": {"hot"}}),
    )

    assert verdict.ok
    assert verdict.reason == "descends-from-release/1.2.3"


def test_the_highest_reachable_release_wins() -> None:
    older = _record(version="1.2.3", sha="older")
    newer = _record(version="1.10.0", sha="newer")
    verdict = release_base.verify_release_base(
        "deployed",
        [older, newer],
        _ancestor_of({"deployed": {"newer"}, "newer": {"older"}}),
    )

    assert verdict.ok
    assert verdict.base is not None and verdict.base.version == "1.10.0"


def test_a_failed_gate_never_makes_its_commit_releasable() -> None:
    verdict = release_base.verify_release_base(
        "deployed",
        [_record(passed=False)],
        _ancestor_of({"deployed": {"gated"}}),
    )

    assert not verdict.ok
    assert verdict.reason == "no-gated-ancestor"


@pytest.mark.parametrize("tag", ["v0.2-eval-driven", "v0.3-control-plane", "app-v1.0.0", "1.2.3"])
def test_milestone_and_foreign_tags_are_not_release_tags(tag: str) -> None:
    assert release_base.parse_release_tag(tag) is None


def test_a_three_part_tag_parses_to_its_version() -> None:
    assert release_base.parse_release_tag("v1.10.0") == (1, 10, 0)


def test_a_malformed_provenance_file_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / "1.2.3.json").write_text(json.dumps({"version": "1.2.3"}), encoding="utf-8")

    with pytest.raises(ValueError, match="missing sha, gate_run, passed"):
        release_base.load_records(tmp_path)


def test_loading_reads_every_record(tmp_path: Path) -> None:
    payload = {"version": "1.2.3", "sha": "gated", "gate_run": "run/1", "passed": True}
    (tmp_path / "1.2.3.json").write_text(json.dumps(payload), encoding="utf-8")

    records = release_base.load_records(tmp_path)

    assert [record.version for record in records] == ["1.2.3"]


# meiva:note
# The door is history, not the working tree: a record that was committed and later deleted must not turn the bootstrap back on, or "delete the file" becomes a way to certify any tree. These three cases are that rule, run against a real repository.
def _git(repo: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603
        ("git", "-C", str(repo), *arguments), check=True, capture_output=True
    )


def _repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "test")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def test_a_repository_that_never_committed_a_record_has_no_history(tmp_path: Path) -> None:
    assert release_base.has_release_history(_repository(tmp_path), "release") is False


def test_a_committed_record_is_history(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "release").mkdir()
    (repo / "release" / "1.0.0.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "release: v1.0.0")

    assert release_base.has_release_history(repo, "release") is True


def test_a_deleted_record_is_still_history(tmp_path: Path) -> None:
    repo = _repository(tmp_path)
    (repo / "release").mkdir()
    (repo / "release" / "1.0.0.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "release: v1.0.0")
    _git(repo, "rm", "-q", "release/1.0.0.json")
    _git(repo, "commit", "-q", "-m", "chore: drop the record")

    assert not (repo / "release").exists() or not list((repo / "release").iterdir())
    assert release_base.has_release_history(repo, "release") is True
