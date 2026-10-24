from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from meizel.tests.synthetic import commit, git

ROOT = Path(__file__).resolve().parents[2]
MEIZEL_PUSH = ROOT / "scripts/hooks/prepush-meizel.sh"


def _init_probe_repo(path: Path) -> None:
    git(path, "init", "-q")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "Test")


def _commit_all(path: Path, message: str) -> None:
    git(path, "add", "-A")
    commit(path, message)


def _init_repo_with_origin_main(path: Path) -> None:
    _init_probe_repo(path)
    (path / "README.md").write_text("base\n")
    _commit_all(path, "base")
    git(path, "update-ref", "refs/remotes/origin/main", "HEAD")


def _run_meizel(repo: Path) -> subprocess.CompletedProcess[str]:
    # meiva:note
    # The command has fixed argv and paths owned by this throwaway repository.
    return subprocess.run(  # noqa: S603
        ["bash", str(MEIZEL_PUSH)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _meizel_probe(repo: Path) -> Path:
    probe = repo / ".venv/bin/python"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        f"#!{sys.executable}\nimport json, pathlib, sys\n"
        "with pathlib.Path('calls.jsonl').open('a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    probe.chmod(0o755)
    (repo / ".gitignore").write_text(".venv/\ncalls.jsonl\n")
    return repo / "calls.jsonl"


@pytest.mark.parametrize("path", ["notes.md", "asset.json"])
def test_meizel_push_skips_a_branch_without_rule_inputs(tmp_path: Path, path: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_origin_main(repo)
    log = _meizel_probe(repo)
    (repo / path).write_text("changed\n")
    _commit_all(repo, "unrelated input")
    result = _run_meizel(repo)
    assert result.returncode == 0
    assert not log.exists()
    assert "skipping meizel" in result.stdout


@pytest.mark.parametrize(
    "path",
    [
        "run.py",
        "meizel.toml",
        "nested/meizel.toml",
        "settings.yaml",
        "settings.yml",
        "scan.sh",
        "pyproject.toml",
        ".pre-commit-config.yaml",
        "space name.yaml",
        "line\nbreak.yaml",
        "-leading.sh",
    ],
)
@pytest.mark.parametrize("change", ["add", "modify", "delete", "rename", "rename-in"])
def test_meizel_push_routes_actual_paths_including_deleted_and_renamed(
    tmp_path: Path, path: str, change: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_probe_repo(repo)
    log = _meizel_probe(repo)
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    source = target.with_name("previous.txt") if change == "rename-in" else target
    if change != "add":
        source.write_text("original\n")
    _commit_all(repo, "base input")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    if change in {"add", "modify"}:
        target.write_text("changed\n")
    elif change == "delete":
        target.unlink()
    elif change == "rename-in":
        source.rename(target)
    else:
        target.rename(target.with_name("renamed.txt"))
    _commit_all(repo, change)
    result = _run_meizel(repo)
    assert result.returncode == 0, result.stderr
    expected = ["-m", "meizel", "check"]
    if not (path.endswith(".py") or target.name == "meizel.toml"):
        expected += ["--changed", f"./{path}"]
    assert [json.loads(row) for row in log.read_text().splitlines()] == [expected]


def test_meizel_push_runs_the_full_check_once_for_mixed_inputs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo_with_origin_main(repo)
    log = _meizel_probe(repo)
    (repo / "settings.yaml").write_text("new: value\n")
    (repo / "logic.py").write_text("VALUE = 1\n")
    _commit_all(repo, "mixed inputs")
    assert _run_meizel(repo).returncode == 0
    assert [json.loads(row) for row in log.read_text().splitlines()] == [["-m", "meizel", "check"]]


def test_meizel_push_rejects_an_unknown_range(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_probe_repo(repo)
    log = _meizel_probe(repo)
    result = _run_meizel(repo)
    assert result.returncode != 0
    assert "requires local origin/main" in result.stderr
    assert not log.exists()


def test_meizel_hook_keeps_role_inputs_without_a_separate_gate() -> None:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text())
    hooks = {h["id"]: h for r in config["repos"] for h in r["hooks"]}
    assert "lint-meizel-roles" not in hooks
    changed = hooks["meizel-check-changed"]
    for path in ["ordinary.py", "scan.sh", "pkg/data.toml", ".pre-commit-config.yaml", "x.yml"]:
        assert re.search(changed["files"], path)
    hook = hooks["meizel-check-full"]
    assert hook["stages"] == ["pre-push"]
    assert hook["always_run"] is True
    assert hook["verbose"] is True
    assert shlex.split(hook["entry"]) == ["bash", "scripts/hooks/prepush-meizel.sh"]
