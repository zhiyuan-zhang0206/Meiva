# meiva:note
# What the controller accepts of a finished worker, as values in and values out.
#
# `services.backup.scheduler.worker` reads the worker's result, hashes and links the
# staged artifact. This module owns the judgements: whether a result has the shape of its
# operation, whether a staged file is a managed regular artifact, whether its digest is the
# one the worker reported, and what a failed exclusive link means.

from __future__ import annotations

import errno
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from services.backup.artifact.names import DUMP_NAME_RE

__all__ = [
    "digest_problem",
    "dump_result_problem",
    "link_route",
    "restore_result_problem",
    "staged_problem",
]


# meiva:note
# Why a dump worker's result cannot be committed, or None: it must carry exactly the
# artifact's bare file name and its SHA-256, and the name must not escape the controls.
def dump_result_problem(result: Mapping[str, object]) -> str | None:
    if set(result) != {"artifact", "sha256"}:
        return "logical backup returned an invalid result"
    name, digest = result["artifact"], result["sha256"]
    if not isinstance(name, str) or Path(name).name != name or not isinstance(digest, str):
        return "logical backup result escaped its controls"
    return None


# meiva:note
# Why a restore worker's result is not the success it must report, or None.
def restore_result_problem(result: Mapping[str, object]) -> str | None:
    if result != {"restored": True}:
        return "logical restore returned an invalid result"
    return None


# meiva:note
# Why a staged file is not a managed regular artifact, or None. A link is never one.
def staged_problem(name: str, *, is_symlink: bool, is_file: bool) -> str | None:
    if is_symlink or not is_file or DUMP_NAME_RE.fullmatch(name) is None:
        return "scheduled backup result is not a managed regular artifact"
    return None


# meiva:note
# Why the staged artifact is no longer the one the worker prepared, or None.
def digest_problem(actual: str, expected: str) -> str | None:
    if actual != expected:
        return "scheduled backup changed after preparation"
    return None


# meiva:note
# What a failed exclusive link means: a backup directory on another filesystem gets a
# verified private copy; every other failure (an existing artifact included) is raised.
def link_route(error_number: int | None) -> Literal["copy", "raise"]:
    return "copy" if error_number == errno.EXDEV else "raise"
