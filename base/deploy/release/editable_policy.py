# meiva:note
# Decide whether the editable Meiva install records of a checkout's virtualenv are legal.
#
# The shell (`editable_install.py`) lists and reads the records and hands them in as values; this
# module only judges them against an exact-root allowlist and words each violation.

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeGuard

__all__ = [
    "EditableReadings",
    "MetadataRecord",
    "PointerRecord",
    "direct_url_is_allowed",
    "editable_violations",
    "pointer_is_allowed",
    "sorted_paths",
]


# meiva:note
# One editable pointer (`.pth`) found in the virtualenv. `text` is None when it could not be read.
# `targets` is the platform-native exact path of each non-blank line, resolved by the reader, or
# None when one could not be resolved.
@dataclass(frozen=True)
class PointerRecord:
    path: Path
    text: str | None
    targets: tuple[str, ...] | None


# meiva:note
# One `direct_url.json` found in the virtualenv; `text` is None when it could not be read.
@dataclass(frozen=True)
class MetadataRecord:
    path: Path
    text: str | None


# meiva:note
# Everything the reader observed about the install: the pointers and metadata records that exist,
# the pointer paths missing beside an existing metadata record, and the existing pointers whose
# site-packages directory has no metadata record.
@dataclass(frozen=True)
class EditableReadings:
    pointers: tuple[PointerRecord, ...]
    metadata: tuple[MetadataRecord, ...]
    pointers_missing: tuple[Path, ...]
    pointers_without_metadata: tuple[Path, ...]


# meiva:note
# The distinct paths of a record listing in the order every listing reports them: by their text.
def sorted_paths(paths: Iterable[Path]) -> tuple[Path, ...]:
    return tuple(sorted(set(paths), key=str))


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


# meiva:note
# Whether every editable-pointer line names an allowlisted exact root.
#
# uv writes one line for each wheel package, so a healthy project with multiple packages can
# legitimately repeat the same checkout path. Pointer legality is therefore semantic rather than a
# particular byte layout.
def pointer_is_allowed(record: PointerRecord, allowed_roots: frozenset[str]) -> bool:
    if record.text is None:
        return False
    lines = record.text.splitlines()
    if not lines or any(not line for line in lines):
        return False
    if record.targets is None:
        return False
    return all(target in allowed_roots for target in record.targets)


# meiva:note
# A `direct_url.json` record is legal when it names an allowed root as an editable file URL.
#
# Anything unparsable, pointing at a non-allowlisted root, or not marked editable is treated as
# poisoned: the editable pointer in the same venv proves this is an editable install, so a record
# that disagrees is stale and a later `uv sync` could install from it.
def direct_url_is_allowed(raw_text: str, allowed_urls: frozenset[str]) -> bool:
    if not raw_text.strip():
        return False
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return False
    if not _is_str_dict(payload):
        return False
    url = payload.get("url")
    dir_info = payload.get("dir_info")
    if not isinstance(url, str) or not _is_str_dict(dir_info):
        return False
    if dir_info.get("editable") is not True:
        return False
    return url.rstrip("/") in allowed_urls


# meiva:note
# Human-readable allowlist violations of the observed install, sorted. A record that could not be
# read is a violation rather than a crash; both editable records missing is a no-op (nothing is
# observed), while a missing pointer or metadata beside its existing sibling is a half-uninstall.
def editable_violations(
    readings: EditableReadings, *, allowed_roots: frozenset[str], allowed_urls: frozenset[str]
) -> tuple[str, ...]:
    violations: list[str] = []
    for path in readings.pointers_missing:
        violations.append(
            f"{path} editable install metadata present but pointer missing — half-uninstalled"
        )
    for path in readings.pointers_without_metadata:
        violations.append(
            f"{path} editable pointer present but metadata missing — half-uninstalled"
        )
    for pointer in readings.pointers:
        if pointer.text is None:
            violations.append(f"{pointer.path} unreadable")
        elif not pointer_is_allowed(pointer, allowed_roots):
            target = pointer.text.strip() or "(empty)"
            violations.append(
                f"{pointer.path} names {target!r}; non-allowlisted source or is inconsistent"
            )
    for record in readings.metadata:
        if record.text is None:
            violations.append(f"{record.path} unreadable")
        elif not direct_url_is_allowed(record.text, allowed_urls):
            violations.append(f"{record.path} records {record.text.strip() or '(empty)'!r}")
    return tuple(sorted(violations))
