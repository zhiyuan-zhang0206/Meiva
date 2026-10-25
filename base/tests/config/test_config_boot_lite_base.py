"""The skip mode defers config preparation and plants the placeholder database URL."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CHILD = Path(__file__).with_name("_config_child.py")


@pytest.fixture
def empty_home(tmp_path: Path) -> Path:
    """Point the children at an empty home: no `.env`, so the skip-mode placeholders apply."""
    home = tmp_path / "empty-home"
    home.mkdir()
    return home


def _clean_env(home: Path, **overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.pop("VIRTUAL_ENV", None)
    # Stripping MEIVA_* must not strip the home: a child without MEIVA_HOME falls back to the
    # operator's `~/.meiva` and boots against its production `.env`.
    env["MEIVA_HOME"] = str(home)
    env.update(overrides)
    return env


def _spawn(
    home: Path, *, env: dict[str, str] | None = None, timeout: float = 180
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — fixed argv, sys.executable owns the child
        [sys.executable, "-B", str(_CHILD), "skip_defers_prepare"],
        check=False,
        cwd=_REPO_ROOT,
        env=_clean_env(home, **(env or {})),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_skip_mode_defers_prepare_and_plants_placeholders(empty_home: Path) -> None:
    proc = _spawn(empty_home, env={"MEIVA_CONFIG_FETCH": "skip"})
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("DEFER False True "), proc.stdout
    assert proc.stdout.rstrip().endswith("True"), proc.stdout
