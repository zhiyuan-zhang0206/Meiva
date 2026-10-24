from __future__ import annotations

import os
import stat
import venv
from pathlib import Path

import pytest

from services.supervision.healthchecks import prod_venv as hc

_HEALTHY_ENV = {"PATH": os.environ.get("PATH", "")}


# meiva:note
# A real virtualenv at `<root>/.venv` whose site-packages holds the four packages the import smoke
# names, as real directories: complete, or hollow (no `__init__.py`), or with one missing.
def _checkout(root: Path, damage: str) -> Path:
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root / ".venv")
    site = next((root / ".venv/lib").glob("python*/site-packages"))
    for name in ("meiva", "pydantic", "psycopg", "fastapi"):
        if name == "psycopg" and damage == "missing":
            continue
        package = site / name
        package.mkdir()
        if damage != "hollow":
            (package / "__init__.py").write_text("")
    return site


def _package_files(site: Path) -> dict[Path, bytes]:
    return {p.relative_to(site): p.read_bytes() for p in site.rglob("*") if p.is_file()}


# meiva:note
# An executable script that stands in for `uv`: it exits with `code` after writing the
# environment variables it was given, or sleeps when `hang` is set.
def _uv(root: Path, *, code: int = 0, hang: bool = False) -> str:
    script = root / "fake-uv"
    body = "sleep 30" if hang else f"env > {root}/uv-env.txt\nexit {code}"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_the_isolated_environment_drops_only_the_variables_that_redirect_imports() -> None:
    kept = hc.isolated_env(
        {"VIRTUAL_ENV": "/other/.venv", "PYTHONPATH": "/other", "PATH": "/bin", "HOME": "/h"}
    )

    assert kept == {"PATH": "/bin", "HOME": "/h"}


def test_the_dependency_check_runs_in_the_isolated_environment(tmp_path: Path) -> None:
    _checkout(tmp_path, "healthy")
    uv = _uv(tmp_path)

    violations = hc.venv_violations(
        source_root=tmp_path,
        uv=uv,
        environment={**_HEALTHY_ENV, "VIRTUAL_ENV": "/other/.venv", "PYTHONPATH": "/other"},
    )

    assert violations == ()
    seen = (tmp_path / "uv-env.txt").read_text()
    assert "VIRTUAL_ENV" not in seen
    assert "PYTHONPATH" not in seen


def test_a_failing_dependency_check_is_a_violation(tmp_path: Path) -> None:
    _checkout(tmp_path, "healthy")

    violations = hc.venv_violations(
        source_root=tmp_path, uv=_uv(tmp_path, code=3), environment=_HEALTHY_ENV
    )

    assert len(violations) == 1
    assert violations[0].startswith("uv pip check failed (rc=3)")


def test_dependency_timeout_does_not_skip_import_check(tmp_path: Path) -> None:
    _checkout(tmp_path, "missing")

    violations = hc.venv_violations(
        source_root=tmp_path, uv=_uv(tmp_path, hang=True), environment=_HEALTHY_ENV, timeout_s=0.5
    )

    assert "timed out" in violations[0]
    assert any("No module named" in violation for violation in violations)


def test_an_unstartable_uv_is_reported_not_raised(tmp_path: Path) -> None:
    _checkout(tmp_path, "healthy")

    violations = hc.venv_violations(
        source_root=tmp_path, uv=str(tmp_path / "no-uv"), environment=_HEALTHY_ENV
    )

    assert len(violations) == 1
    assert "could not start" in violations[0]


def test_missing_python_does_not_spawn_a_diagnostic(tmp_path: Path) -> None:
    uv = _uv(tmp_path)

    violations = hc.venv_violations(source_root=tmp_path, uv=uv, environment=_HEALTHY_ENV)

    assert "venv python missing" in violations[0]
    assert not (tmp_path / "uv-env.txt").exists()


@pytest.mark.parametrize("damage", ["healthy", "missing", "hollow"])
def test_real_isolated_import_detects_damage_without_writes(tmp_path: Path, damage: str) -> None:
    site = _checkout(tmp_path, damage)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "psycopg.py").write_text("")
    before = _package_files(site)

    violations = hc.venv_violations(
        source_root=tmp_path,
        uv=None,
        environment={**_HEALTHY_ENV, "PYTHONPATH": str(shadow)},
    )

    assert before == _package_files(site)
    if damage == "healthy":
        assert violations == ()
    else:
        message = violations[0]
        if damage == "missing":
            assert "No module named" in message
            assert "psycopg" in message
        else:
            assert "meiva: hollow package" in message
