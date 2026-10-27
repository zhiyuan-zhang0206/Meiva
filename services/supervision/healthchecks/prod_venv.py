"""Read-only dependency and import diagnostics for an explicit checkout virtualenv."""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from base.deploy.release import editable_install
from base.host import proc

_log = logging.getLogger("services.supervision.healthchecks.prod_venv")

_STDERR_TAIL_CHARS = 1000
_IMPORT_SMOKE = """\
import meiva, pydantic, psycopg, fastapi
for module in (meiva, pydantic, psycopg, fastapi):
    if module.__file__ is None:
        raise ImportError(f"{module.__name__}: hollow package (namespace without __init__.py)")
"""


def _stderr_tail(stderr: str | bytes | None) -> str:
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    # uv's elapsed-time summary changes every round even for identical damage.
    # --quiet also hides incompatibility details, so strip only this summary.
    stderr = re.sub(r"(?m)^Checked \d+ packages? in [^\n]+\n?", "", stderr)
    return stderr[-_STDERR_TAIL_CHARS:].strip()


# meiva:note
# One subcommand's failure line and whether it says anything about the environment. Only a leg that
# ran to completion and answered with a violation is evidence; ran out of its deadline, could not
# start (EAGAIN/ENOMEM/EACCES on a loaded host, a missing interpreter) and killed without a return
# code (an OOM kill) are all "never read" — the shapes a busy machine produces, and the ones that
# must not read as damage (2026-10-27, task #5656).
@dataclass(frozen=True, slots=True)
class _LegFailure:
    detail: str
    unmeasured: bool


# meiva:note
# One subcommand inside the observation's deadline: it may spend an even share of whatever the legs
# before it left (`legs_left` counts itself), so a hung leg can never starve the ones after it, and
# the whole observation still ends inside the caller's deadline. A leg whose share is already spent
# is never spawned and reports that it was never read.
def _probe_within(
    label: str, argv: list[str], env: dict[str, str], deadline: float, *, legs_left: int
) -> _LegFailure | None:
    share = (deadline - time.monotonic()) / legs_left
    if share <= 0:
        return _LegFailure(f"{label}: the observation deadline was already spent", unmeasured=True)
    return _probe(label, argv, env, share)


def _probe(
    label: str, argv: list[str], env: dict[str, str], timeout_s: float
) -> _LegFailure | None:
    """Return a bounded diagnostic for failed execution, including spawn failure."""
    try:
        result = proc.run_bounded(
            argv,
            timeout=timeout_s,
            cwd=tempfile.gettempdir(),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return _LegFailure(
            f"{label} did not finish within its share of the observation deadline; "
            f"stderr={_stderr_tail(exc.stderr)!r}",
            unmeasured=True,
        )
    except OSError as exc:
        return _LegFailure(f"{label} could not start: {exc}", unmeasured=True)
    if result.returncode < 0:
        return _LegFailure(
            f"{label} was killed by signal {-result.returncode} before answering; "
            f"stderr={_stderr_tail(result.stderr)!r}",
            unmeasured=True,
        )
    if result.returncode:
        return _LegFailure(
            f"{label} failed (rc={result.returncode}); stderr={_stderr_tail(result.stderr)!r}",
            unmeasured=False,
        )
    return None


# meiva:note
# The environment the isolated checks run in: the process environment without the variables that
# would point the child at another virtualenv or another import path.
def isolated_env(environment: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in environment.items() if k not in {"VIRTUAL_ENV", "PYTHONPATH"}}


# meiva:note
# One read of the virtualenv of the checkout at `source_root`: what the legs found wrong, and what
# they could not read at all. Damage and a missing reading are kept apart because only the first
# is evidence about the environment — a machine too busy to finish a leg says nothing about it.
@dataclass(frozen=True, slots=True)
class VenvObservation:
    violations: tuple[str, ...]
    unmeasured: tuple[str, ...]


# meiva:note
# The observation of the virtualenv at `source_root`. The caller hands over what the legs need:
# `uv` (the resolved uv executable, or None when uv is not installed and the dependency leg is
# skipped), `environment` (the process environment, which is isolated here) and `deadline_s` (the
# whole deadline for the observation: the legs share it, so a hung metadata check cannot skip the
# imports — it only spends them, and the import leg then reports that it ran out of time instead of
# pretending the environment is damaged).
def observe_venv(
    *,
    source_root: Path,
    uv: str | None,
    environment: Mapping[str, str],
    deadline_s: float,
) -> VenvObservation:
    interpreter = editable_install.venv_python(source_root)
    if interpreter is None:
        return VenvObservation((f"{source_root}: venv python missing",), ())

    env = isolated_env(environment)
    violations: list[str] = []
    unmeasured: list[str] = []
    deadline = time.monotonic() + deadline_s
    if uv is None:
        _log.debug("[prod-venv healthcheck] uv not on PATH; skipping dependency check")
    else:
        failure = _probe_within(
            "uv pip check",
            [uv, "pip", "check", "--python", str(interpreter)],
            env,
            deadline,
            legs_left=2,
        )
        if failure is not None:
            (unmeasured if failure.unmeasured else violations).append(failure.detail)
    # meiva:note
    # -I excludes the parent's PYTHON* settings, cwd and user site-packages; -B prevents this
    # read-only check from writing bytecode into the venv.
    failure = _probe_within(
        "import smoke",
        [str(interpreter), "-I", "-B", "-c", _IMPORT_SMOKE],
        env,
        deadline,
        legs_left=1,
    )
    if failure is not None:
        (unmeasured if failure.unmeasured else violations).append(failure.detail)
    return VenvObservation(tuple(violations), tuple(unmeasured))
