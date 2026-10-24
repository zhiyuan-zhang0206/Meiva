"""Read-only dependency and import diagnostics for an explicit checkout virtualenv."""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from base.deploy.release import editable_install
from base.host import proc

_log = logging.getLogger("services.supervision.healthchecks.prod_venv")

# Each leg has its own deadline, so a hung metadata check cannot skip imports.
_PROBE_TIMEOUT_S = 5.0
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


def _probe(label: str, argv: list[str], env: dict[str, str], timeout_s: float) -> str | None:
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
        return f"{label} timed out after {timeout_s:g}s; stderr={_stderr_tail(exc.stderr)!r}"
    except OSError as exc:
        return f"{label} could not start: {exc}"
    if result.returncode:
        return f"{label} failed (rc={result.returncode}); stderr={_stderr_tail(result.stderr)!r}"
    return None


# meiva:note
# The environment the isolated checks run in: the process environment without the variables that
# would point the child at another virtualenv or another import path.
def isolated_env(environment: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in environment.items() if k not in {"VIRTUAL_ENV", "PYTHONPATH"}}


# meiva:note
# The dependency and import violations of the virtualenv of the checkout at `source_root`. The
# root hands over what the checks need: `uv` (the resolved uv executable, or None when uv is not
# installed and the dependency leg is skipped) and `environment` (the process environment, which
# is isolated here). Each leg has its own `timeout_s`, so a hung metadata check cannot skip the
# imports.
def venv_violations(
    *,
    source_root: Path,
    uv: str | None,
    environment: Mapping[str, str],
    timeout_s: float = _PROBE_TIMEOUT_S,
) -> tuple[str, ...]:
    interpreter = editable_install.venv_python(source_root)
    if interpreter is None:
        return (f"{source_root}: venv python missing",)

    env = isolated_env(environment)
    violations: list[str] = []
    if uv is None:
        _log.debug("[prod-venv healthcheck] uv not on PATH; skipping dependency check")
    else:
        failure = _probe(
            "uv pip check", [uv, "pip", "check", "--python", str(interpreter)], env, timeout_s
        )
        if failure is not None:
            violations.append(failure)
    # -I excludes the parent's PYTHON* settings, cwd and user site-packages;
    # -B prevents this read-only check from writing bytecode into the venv.
    failure = _probe(
        "import smoke", [str(interpreter), "-I", "-B", "-c", _IMPORT_SMOKE], env, timeout_s
    )
    if failure is not None:
        violations.append(failure)
    return tuple(violations)
