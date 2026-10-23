# meiva:note
# The ruff stages as values: the argv each stage runs, how a stage's exit status and output become
# its result, and the warning a stage that gave up logs. Every stage passes the source through
# unchanged when ruff cannot help, so none of these raises.
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

RUFF_TIMEOUT_SECONDS = 5

_F821_MSG_RE = re.compile(r"Undefined name `([^`]+)`")


@dataclass(frozen=True)
class RuffRun:
    executable: str
    timeout_seconds: float = RUFF_TIMEOUT_SECONDS


class FailureKind(StrEnum):
    TIMEOUT = "timeout"
    MISSING = "missing"
    UNICODE = "unicode"
    OS_ERROR = "os_error"


@dataclass(frozen=True)
class RuffFailure:
    kind: FailureKind
    timeout: float | None = None
    errno: int | None = None
    strerror: str | None = None
    error_type: str = ""
    error_text: str = ""


def fix_argv(ruff: RuffRun) -> tuple[str, ...]:
    return (
        ruff.executable,
        "check",
        "--fix",
        "--stdin-filename",
        "script.py",
        "--quiet",
        "-",
    )


def format_argv(ruff: RuffRun) -> tuple[str, ...]:
    return (ruff.executable, "format", "--stdin-filename", "script.py", "-")


def undefined_names_argv(ruff: RuffRun) -> tuple[str, ...]:
    return (
        ruff.executable,
        "check",
        "--select",
        "F821",
        "--output-format",
        "json",
        "--stdin-filename",
        "script.py",
        "-",
    )


# meiva:note
# What `ruff check --fix` and `ruff format` leave: their stdout when they exited cleanly with
# output, otherwise the source unchanged (an empty result is never a rewrite).
def rewritten_source(code: str, returncode: int, stdout: str) -> str:
    if returncode != 0 or not stdout.strip():
        return code
    return stdout


# meiva:note
# The undefined names of `ruff check --select F821 --output-format json`. Exit 1 means diagnostics
# were found; above 1 ruff crashed. A crash, empty output or output that is not JSON names nothing.
def undefined_names(returncode: int, stdout: str) -> frozenset[str]:
    if returncode > 1 or not stdout.strip():
        return frozenset()
    try:
        diagnostics: list[dict[str, Any]] = json.loads(stdout)
    except json.JSONDecodeError:
        return frozenset()
    names: set[str] = set()
    for diagnostic in diagnostics:
        if diagnostic["code"] != "F821":
            continue
        match = _F821_MSG_RE.search(diagnostic["message"])
        if match:
            names.add(match.group(1))
    return frozenset(names)


# meiva:note
# The warning for a stage that gave up. A timeout names the budget and the input size, a missing
# executable is logged for each skipped stage, a text codec failure names the codec error, and any
# other OS error names its errno.
def give_up_warning(step: str, source_chars: int, failure: RuffFailure, executable: str) -> str:
    match failure.kind:
        case FailureKind.TIMEOUT:
            return (
                f"ruff {step} did not finish within {failure.timeout}s on "
                f"{source_chars}-char source — passing through unchanged"
            )
        case FailureKind.MISSING:
            return (
                f"ruff executable {executable!r} not found — syntax-fix "
                "ruff stage skipped (source passed through unchanged)"
            )
        case FailureKind.UNICODE:
            return (
                f"ruff {step} failed with {failure.error_type} ({failure.error_text}) "
                "— passing through unchanged"
            )
        case FailureKind.OS_ERROR:
            return (
                f"ruff {step} failed with OSError errno={failure.errno} "
                f"({failure.strerror}) — passing through unchanged"
            )
