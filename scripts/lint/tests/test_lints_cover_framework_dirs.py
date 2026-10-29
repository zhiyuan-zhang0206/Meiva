from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from meizel import lint_common
from meizel.checkout import roles_at
from meizel.roles import select_packages
from meizel.tests.synthetic import STANDALONE_PROJECT
from scripts.lint import async_no_sync_blocking, termination_source
from scripts.lint.diagnostics import logger_add_diagnose, loguru_format, no_emoji, time_bomb

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize(
    "scan_dirs",
    [
        select_packages(roles_at(_REPO_ROOT), "application", "tooling"),
        no_emoji.SCAN_DIRS,
        termination_source.SCAN_DIRS,
        logger_add_diagnose.SCAN_DIRS,
        loguru_format.SCAN_DIRS,
    ],
)
def test_framework_scoped_lints_cover_every_framework_dir(scan_dirs: tuple[str, ...]) -> None:
    assert set(lint_common.FRAMEWORK_DIRS) <= set(scan_dirs)
    lint_common.scan_roots(_REPO_ROOT, scan_dirs)


@pytest.mark.parametrize("directory", (*lint_common.FRAMEWORK_DIRS, "scripts", "meizel"))
def test_time_bomb_default_scan_covers_every_framework_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], directory: str
) -> None:
    for name in (*lint_common.FRAMEWORK_DIRS, "scripts", "meizel", "tests"):
        (tmp_path / name).mkdir()
    target = tmp_path / directory / "tests" / "test_window.py"
    target.parent.mkdir()
    target.write_text('since = "2026-09-06"\n', encoding="utf-8")
    assert time_bomb.main([], repo_root=tmp_path) == 1
    assert f"{target}:1: time-bomb fixture date" in capsys.readouterr().err


def _checker_checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copytree(
        _REPO_ROOT / "meizel",
        root / "meizel",
        ignore=shutil.ignore_patterns("__pycache__", "docs"),
    )
    shutil.copytree(
        _REPO_ROOT / "scripts/structure",
        root / "scripts/structure",
        ignore=shutil.ignore_patterns("__pycache__", "baseline"),
    )
    (root / "scripts/lint").mkdir(parents=True)
    for name in ("code_structure.py", "async_no_sync_blocking.py"):
        shutil.copyfile(_REPO_ROOT / "scripts/lint" / name, root / "scripts/lint" / name)
    files = {
        "meizel.toml": '[roles]\nstandalone = ["engines"]\ntooling = ["meizel", "scripts"]\n',
        "meizel.policy.toml": "",
        ".gitignore": ".venv/\nvendor/\n__pycache__/\n",
        "scripts/structure/baseline/README.md": "Empty fixture baseline.\n",
        "engines/embed/pyproject.toml": STANDALONE_PROJECT.replace(
            'include = ["src", "tests"]', 'include = ["src", "tests", "vendor"]'
        ),
        "engines/embed/uv.lock": "",
        "engines/embed/src/meiva_embedding_service/__init__.py": "",
        "engines/embed/src/meiva_embedding_service/blocking_helpers.py": "\n".join(
            f"def {name}():\n    pass\n"
            for name in sorted(async_no_sync_blocking.REPO_BLOCKING_HELPERS)
        ),
        "engines/embed/tests/test_service.py": "",
    }
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    _command(root, ["git", "init", "-q"])
    _command(root, ["git", "add", "."])
    _command(
        root,
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    )
    return root


# meiva:note
# Fixed Git argv run in a temporary fixture checkout without a shell.
def _command(root: Path, command: list[str]) -> str:
    result = subprocess.run(command, cwd=root, text=True, capture_output=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


# meiva:note
# A fresh interpreter binds the unmodified checker to the fixture roles, not this repository.
def _gate(root: Path, lint: str, only: list[str]) -> tuple[int, str]:
    environment = {**os.environ, "LINT_STRUCTURE_BASELINE_BASE": "HEAD"}
    if lint == "structure":
        result = subprocess.run(  # noqa: S603
            [sys.executable, "scripts/lint/code_structure.py", *only],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    else:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "scripts/lint/async_no_sync_blocking.py", *only],
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
    return result.returncode, result.stdout + result.stderr


def _assert_production_rejections(output: str, filename: str) -> None:
    for expected in (filename, "TYPE_CHECKING", "Task", "800", "complex"):
        assert expected in output, output


@pytest.mark.parametrize("only", [False, True])
def test_standalone_default_and_only_gates_keep_production_rules(
    tmp_path: Path, only: bool
) -> None:
    root = _checker_checkout(tmp_path)
    target = "engines/embed/src/meiva_embedding_service/new.py"
    selected = ["--only", target] if only else []
    status, output = _gate(root, "structure", [] if not only else ["--only"])
    assert status == 0, output
    bad = (
        "from typing import TYPE_CHECKING\nimport asyncio\nimport subprocess\n"
        "if TYPE_CHECKING:\n    import decimal\n"
        "async def work():\n    asyncio.create_task(work())\n    subprocess.run(['true'])\n"
        "def complex(x):\n"
        + "".join(f"    if x == {index}:\n        return {index}\n" for index in range(15))
        + "\n" * 801
    )
    (root / target).write_text(bad)
    status, output = _gate(root, "structure", selected)
    assert status == 1, output
    _assert_production_rejections(output, "new.py")
    status, output = _gate(root, "async", selected)
    assert status == 1 and "subprocess.run" in output, output
    (root / target).write_text("value = 1\n")
    for relative in (".venv/vendor.py", "vendor/ignored.py"):
        sentinel = root / "engines/embed" / relative
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(bad)
    for lint in ("structure", "async"):
        status, output = _gate(root, lint, selected)
        assert status == 0, output
    forced = "engines/embed/vendor/forced.py"
    (root / forced).write_text(bad)
    _command(root, ["git", "add", "-f", forced])
    status, output = _gate(root, "structure", ["--only", forced] if only else [])
    assert status == 1, output
    _assert_production_rejections(output, "forced.py")
