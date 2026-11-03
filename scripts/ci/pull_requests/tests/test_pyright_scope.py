# meiva:note
# pull_requests/pyright_scope.py keeps the impacted pyright run inside the full run's scope.

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.pull_requests import pyright_scope

_PYPROJECT = """\
[tool.pyright]
include = ["base", "scripts", "tests"]
exclude = [
    "**/__pycache__",
    "base/skills/*/*/references",
]
"""


def test_paths_under_an_include_root_are_kept_and_the_rest_dropped() -> None:
    kept, skipped = pyright_scope.filter_paths(
        [
            "base/x.py",
            "scripts/ci/y.py",
            "tests/z.py",
            "schedules/agent-watch-schedule.py",
            "demos/permission-hooks/sensitive_op_gate.py",
            "basement/w.py",
        ],
        _PYPROJECT,
    )
    assert kept == ["base/x.py", "scripts/ci/y.py", "tests/z.py"]
    assert skipped == [
        "schedules/agent-watch-schedule.py",
        "demos/permission-hooks/sensitive_op_gate.py",
        "basement/w.py",
    ]


def test_an_exclude_glob_drops_what_it_matches_and_everything_below() -> None:
    kept, skipped = pyright_scope.filter_paths(
        [
            "base/a/__pycache__/m.py",
            "base/skills/web/ai/references/example.py",
            "base/skills/web/ai/scripts/run.py",
            "base/skills/web/references/example.py",
        ],
        _PYPROJECT,
    )
    assert kept == ["base/skills/web/ai/scripts/run.py", "base/skills/web/references/example.py"]
    assert skipped == ["base/a/__pycache__/m.py", "base/skills/web/ai/references/example.py"]


def _run(stdin: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(_PYPROJECT, encoding="utf-8")
    paths = tmp_path / "impacted.txt"
    paths.write_text(stdin, encoding="utf-8")
    assert pyright_scope.main([str(paths), "--pyproject", str(pyproject)]) == 0
    captured = capsys.readouterr()
    return captured.out, captured.err


def test_an_empty_input_gives_empty_output_and_no_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run("", tmp_path, capsys) == ("", "")
    assert _run("\n\n", tmp_path, capsys) == ("", "")


def test_the_skipped_paths_are_counted_and_listed_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, err = _run("base/x.py\nschedules/a.py\ndemos/b.py\n", tmp_path, capsys)
    assert out == "base/x.py\n"
    assert err == (
        "2 impacted module(s) outside pyright include skipped: schedules/a.py, demos/b.py\n"
    )


def test_a_fully_included_closure_reports_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out, err = _run("base/x.py\ntests/y.py\n", tmp_path, capsys)
    assert out == "base/x.py\ntests/y.py\n"
    assert err == ""


def test_the_default_pyproject_is_the_repository_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = tmp_path / "impacted.txt"
    paths.write_text("base/db/__init__.py\nschedules/a.py\n", encoding="utf-8")
    assert pyright_scope.main([str(paths)]) == 0
    captured = capsys.readouterr()
    assert captured.out == "base/db/__init__.py\n"
    assert captured.err == "1 impacted module(s) outside pyright include skipped: schedules/a.py\n"


def test_the_real_pyproject_scope_matches_the_directories_the_full_run_checks() -> None:
    text = (Path(__file__).resolve().parents[4] / "pyproject.toml").read_text(encoding="utf-8")
    kept, skipped = pyright_scope.filter_paths(
        ["base/db/__init__.py", "schedules/agent-watch-schedule.py", "demos/x.py"], text
    )
    assert kept == ["base/db/__init__.py"]
    assert skipped == ["schedules/agent-watch-schedule.py", "demos/x.py"]
