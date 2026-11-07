"""Declared file and directory inputs checked before every native unit birth.

These seals detect changed configuration; they do not turn mutable paths into
immutable artifacts. Publication must separately exclude concurrent writers.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.host.verified_file.plan import stat_changed


class InputChangedError(OSError):
    """A declared input cannot authorize another birth of this generation."""


def _member_digest(path: Path) -> str:
    before = path.lstat()
    if stat.S_ISREG(before.st_mode):
        value: object = ["file", hashlib.sha256(path.read_bytes()).hexdigest()]
    elif stat.S_ISDIR(before.st_mode):
        value = ["directory", [(p.name, _member_digest(p)) for p in sorted(path.iterdir())]]
    else:
        raise InputChangedError(f"service input must be a regular file or directory: {path}")
    after = path.lstat()
    if stat_changed(before, after):
        raise InputChangedError(f"service input changed while reading: {path}")
    return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class InputSeal:
    """One canonical path and the expected identity of its complete contents."""

    path: Path
    digest: str

    @classmethod
    def capture(cls, path: Path) -> InputSeal:
        if not path.is_absolute() or path.resolve(strict=True) != path:
            raise InputChangedError(f"service input path must be absolute and canonical: {path}")
        return cls(path, _member_digest(path))

    @classmethod
    def from_mapping(cls, raw: object) -> InputSeal:
        if not is_object_dict(raw) or set(raw) != {"path", "digest"}:
            raise ValueError("service input must contain exactly path and digest")
        path, digest = raw["path"], raw["digest"]
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise ValueError("service input path must be absolute")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("service input digest must be a lowercase SHA256")
        return cls(Path(path), digest)

    def as_mapping(self) -> dict[str, str]:
        return {"path": str(self.path), "digest": self.digest}

    def require_unchanged(self) -> None:
        if self.capture(self.path) != self:
            raise InputChangedError(
                f"service input changed since generation admission: {self.path}"
            )


# meiva:note
# The sealed paths whose contents are no longer the ones admitted. Asking this before a spawn is
# what turns "the unit cannot be revived" into a fact with a name (task #6403).
def diverged_inputs(seals: Iterable[InputSeal]) -> tuple[str, ...]:
    drift: list[str] = []
    for seal in seals:
        try:
            seal.require_unchanged()
        except InputChangedError:
            drift.append(str(seal.path))
    return tuple(drift)


def parse_inputs(raw: object) -> tuple[InputSeal, ...]:
    if not is_object_list(raw):
        raise TypeError("service inputs must be a list")
    inputs = tuple(InputSeal.from_mapping(item) for item in raw)
    if len({item.path for item in inputs}) != len(inputs):
        raise ValueError("service inputs contain duplicate paths")
    return inputs


# meiva:note
# Re-capture every seal of one unit, answering both what the new seals are and what changed:
# (path, admitted digest, current digest) for each input whose contents moved (task #6403).
# Re-admission is the one explicit path by which a running root may accept contents it did not
# admit at start; the caller decides what that means for the unit.
def readmit_seals(
    seals: Iterable[InputSeal],
) -> tuple[tuple[InputSeal, ...], tuple[tuple[str, str, str], ...]]:
    admitted = tuple(seals)
    current = tuple(InputSeal.capture(seal.path) for seal in admitted)
    changed = tuple(
        (str(old.path), old.digest, new.digest)
        for old, new in zip(admitted, current, strict=True)
        if old.digest != new.digest
    )
    return current, changed
