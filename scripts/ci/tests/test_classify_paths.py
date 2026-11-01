import subprocess
from pathlib import Path

import pytest

from scripts.ci.pull_requests.classify_paths import main


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
    (repo / "tests" / "test_sample.py").write_text("def test_ok() -> None:\n    assert True\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-q",
            "-m",
            "init",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def lanes(
    scratch: Path, paths: list[str], head_ref: str, capsys: pytest.CaptureFixture[str]
) -> dict[str, str]:
    repo = _make_repo(scratch)
    (scratch / "changed.txt").write_text("\n".join(paths) + "\n")
    (scratch / "base.txt").write_text("HEAD\n")
    main(scratch, head_ref, repo_root=repo)
    return dict(line.split("=") for line in capsys.readouterr().out.splitlines())


def test_a_backend_change_needs_the_backend_lane(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lanes(tmp_path, ["agent/graph/exec/node.py"], "feature", capsys)["backend"] == "true"


def test_a_documentation_only_change_skips_both_lanes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    found = lanes(tmp_path, ["README.md"], "trunk-merge/pr-1/abc", capsys)
    assert found == {"frontend": "false", "backend": "false"}


def test_the_output_is_two_lines_for_github_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    found = lanes(tmp_path, ["agent/graph/exec/node.py"], "feature", capsys)
    assert set(found) == {"frontend", "backend"}
    assert set(found.values()) <= {"true", "false"}
