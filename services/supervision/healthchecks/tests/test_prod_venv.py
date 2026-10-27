from __future__ import annotations

import os
import stat
import subprocess
import venv
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from base.clock import Clock, ClockConfig
from base.clock.tests.manual_timers import ManualTimers
from base.host.proc import CommandRunner, run_bounded
from services.supervision.healthchecks import prod_venv as hc

_HEALTHY_ENV = {"PATH": os.environ.get("PATH", "")}
# meiva:note
# The whole observation deadline the legs share. The caller owns it — nothing in the check falls
# back to a literal.
_DEADLINE_S = 5.0
# meiva:note
# The clock the observation measures its deadline on: frozen, so the legs' shares are the deadline
# divided by the legs left and nothing the machine's load does changes them.
_CLOCK = Clock(ClockConfig("UTC", None, message_timestamp_weekday=False), timers=ManualTimers())


# meiva:note
# A runner that ends the dependency leg as `run_bounded` does once its deadline kills the tree (it
# raises `TimeoutExpired`, at once and without waiting), and runs every other leg for real.
def _runner_that_hangs_on(uv: str) -> CommandRunner:
    def run(
        argv: Sequence[str],
        *,
        timeout: float,
        capture_output: bool = False,
        input: bytes | str | None = None,
        **popen_kwargs: object,
    ) -> subprocess.CompletedProcess[Any]:
        if argv[0] == uv:
            raise subprocess.TimeoutExpired(list(argv), timeout)
        return run_bounded(
            argv, timeout=timeout, capture_output=capture_output, input=input, **popen_kwargs
        )

    return run


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
# environment variables it was given.
def _uv(root: Path, *, code: int = 0) -> str:
    script = root / "fake-uv"
    body = f"env > {root}/uv-env.txt\nexit {code}"
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

    observation = hc.observe_venv(
        source_root=tmp_path,
        uv=uv,
        environment={**_HEALTHY_ENV, "VIRTUAL_ENV": "/other/.venv", "PYTHONPATH": "/other"},
        deadline_s=_DEADLINE_S,
        run=run_bounded,
        clock=_CLOCK,
    )

    assert observation == hc.VenvObservation((), ())
    seen = (tmp_path / "uv-env.txt").read_text()
    assert "VIRTUAL_ENV" not in seen
    assert "PYTHONPATH" not in seen


def test_a_failing_dependency_check_is_a_violation(tmp_path: Path) -> None:
    _checkout(tmp_path, "healthy")

    observation = hc.observe_venv(
        source_root=tmp_path,
        uv=_uv(tmp_path, code=3),
        environment=_HEALTHY_ENV,
        deadline_s=_DEADLINE_S,
        run=run_bounded,
        clock=_CLOCK,
    )

    (violation,) = observation.violations
    assert violation.startswith("uv pip check failed (rc=3)")
    assert observation.unmeasured == ()


# meiva:note
# The split a busy host needs: a leg that ran out of its deadline is *unmeasured*, never a
# violation, and the legs after it still run and still report what they find (2026-10-27, #5656).
def test_dependency_timeout_is_unmeasured_and_does_not_skip_import_check(tmp_path: Path) -> None:
    _checkout(tmp_path, "missing")
    uv = _uv(tmp_path)

    observation = hc.observe_venv(
        source_root=tmp_path,
        uv=uv,
        environment=_HEALTHY_ENV,
        deadline_s=_DEADLINE_S,
        run=_runner_that_hangs_on(uv),
        clock=_CLOCK,
    )

    (unmeasured,) = observation.unmeasured
    assert unmeasured.startswith(
        "uv pip check did not finish within its share of the observation deadline"
    )
    (violation,) = observation.violations
    assert "No module named" in violation


# meiva:note
# A leg that could not start says nothing about the environment: a loaded host answers fork/exec with
# EAGAIN, ENOMEM or EACCES, and a missing interpreter is ENOENT. Reading that as damage is the false
# alarm this whole change removes, so it is "never read", and only a run that finished with a
# violation stays in `violations` (2026-10-27, task #5656).
def test_an_unstartable_uv_is_unmeasured_not_a_violation(tmp_path: Path) -> None:
    _checkout(tmp_path, "healthy")

    observation = hc.observe_venv(
        source_root=tmp_path,
        uv=str(tmp_path / "no-uv"),
        environment=_HEALTHY_ENV,
        deadline_s=_DEADLINE_S,
        run=run_bounded,
        clock=_CLOCK,
    )

    (unmeasured,) = observation.unmeasured
    assert "uv pip check could not start" in unmeasured
    assert observation.violations == ()


# meiva:note
# The same rule for a leg the kernel ended instead of the work: an OOM kill answers with a negative
# return code and no verdict, which is what a loaded host does to a heavy import.
def test_a_killed_leg_is_unmeasured_not_a_violation(tmp_path: Path) -> None:
    _checkout(tmp_path, "healthy")
    killer = tmp_path / "killing-uv"
    killer.write_text("#!/bin/sh\nkill -9 $$\n")
    killer.chmod(0o700)

    observation = hc.observe_venv(
        source_root=tmp_path,
        uv=str(killer),
        environment=_HEALTHY_ENV,
        deadline_s=_DEADLINE_S,
        run=run_bounded,
        clock=_CLOCK,
    )

    (unmeasured,) = observation.unmeasured
    assert "uv pip check was killed by signal 9 before answering" in unmeasured
    assert observation.violations == ()


def test_missing_python_does_not_spawn_a_diagnostic(tmp_path: Path) -> None:
    uv = _uv(tmp_path)

    observation = hc.observe_venv(
        source_root=tmp_path,
        uv=uv,
        environment=_HEALTHY_ENV,
        deadline_s=_DEADLINE_S,
        run=run_bounded,
        clock=_CLOCK,
    )

    assert "venv python missing" in observation.violations[0]
    assert observation.unmeasured == ()
    assert not (tmp_path / "uv-env.txt").exists()


@pytest.mark.parametrize("damage", ["healthy", "missing", "hollow"])
def test_real_isolated_import_detects_damage_without_writes(tmp_path: Path, damage: str) -> None:
    site = _checkout(tmp_path, damage)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "psycopg.py").write_text("")
    before = _package_files(site)

    observation = hc.observe_venv(
        source_root=tmp_path,
        uv=None,
        environment={**_HEALTHY_ENV, "PYTHONPATH": str(shadow)},
        deadline_s=_DEADLINE_S,
        run=run_bounded,
        clock=_CLOCK,
    )

    assert before == _package_files(site)
    assert observation.unmeasured == ()
    if damage == "healthy":
        assert observation.violations == ()
    else:
        message = observation.violations[0]
        if damage == "missing":
            assert "No module named" in message
            assert "psycopg" in message
        else:
            assert "meiva: hollow package" in message
