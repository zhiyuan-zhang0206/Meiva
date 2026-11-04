# meiva:note
# What a sealed release tree is, as values: one manifest entry per path (kind, mode, size, inode,
# mtime, ctime, symlink target, content digest), the manifest's JSON form, where a release and its
# manifest live, and what differs between a manifest and a tree as observed. `sealed_tree.py`
# reads and writes the disk; nothing here touches it.
# meiva:link future/infra/lifecycle/release-dir-atomic-code-swap.md

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeGuard

from base.host.boundary.untyped import object_of

MANIFEST_SCHEMA = "meiva-sealed-tree/v1"
MAX_REPORTED = 50


# meiva:note
# What one path of a sealed tree is; the value is the manifest's persisted string.
class EntryKind(StrEnum):
    FILE = "file"
    DIR = "dir"
    LINK = "link"


_SHA256 = re.compile(r"[0-9a-f]{64}")
_MANIFEST_FIELDS = frozenset({"schema", "root", "entries"})
_STAT_FIELDS = ("kind", "mode", "size", "inode", "mtime_ns", "ctime_ns", "target")
_ROW_LENGTH = 8


class ManifestError(ValueError):
    pass


# meiva:note
# One path of a sealed tree as `lstat` saw it after sealing. The kernel moves `ctime_ns` on every
# write, `chmod` or flag change and no unprivileged call sets it back, so the stat fields alone
# find any change without reading content; `sha256` is the content check, present for a file whose
# bytes were read and None otherwise.
@dataclass(frozen=True)
class Entry:
    kind: EntryKind
    mode: int
    size: int
    inode: int
    mtime_ns: int
    ctime_ns: int
    target: str | None = None
    sha256: str | None = None

    def as_row(self) -> list[object]:
        return [
            self.kind,
            self.mode,
            self.size,
            self.inode,
            self.mtime_ns,
            self.ctime_ns,
            self.target,
            self.sha256,
        ]

    @classmethod
    def from_row(cls, raw: object, *, origin: str) -> Entry:
        if not _is_list(raw) or len(raw) != _ROW_LENGTH:
            raise ManifestError(f"{origin}: an entry is a list of {_ROW_LENGTH} values")
        kind = _entry_kind(raw[0], origin=origin)
        mode, size, inode, mtime_ns, ctime_ns = (_count(value, origin=origin) for value in raw[1:6])
        return cls(
            kind,
            mode,
            size,
            inode,
            mtime_ns,
            ctime_ns,
            target=_target(kind, raw[6], origin=origin),
            sha256=_digest(kind, raw[7], origin=origin),
        )


def _is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_str_dict(value: object) -> TypeGuard[dict[str, object]]:
    return _is_object_dict(value) and all(isinstance(key, str) for key in value)


def _entry_kind(value: object, *, origin: str) -> EntryKind:
    try:
        return EntryKind(value)
    except ValueError as exc:
        kinds = [kind.value for kind in EntryKind]
        raise ManifestError(f"{origin}: kind {value!r} is not one of {kinds}") from exc


def _count(value: object, *, origin: str) -> int:
    if type(value) is not int or value < 0:
        raise ManifestError(f"{origin}: {value!r} is not a non-negative integer")
    return value


def _target(kind: EntryKind, value: object, *, origin: str) -> str | None:
    if kind is not EntryKind.LINK:
        if value is not None:
            raise ManifestError(f"{origin}: only a link entry has a target")
        return None
    if not isinstance(value, str):
        raise ManifestError(f"{origin}: a link entry names its target")
    return value


def _digest(kind: EntryKind, value: object, *, origin: str) -> str | None:
    if value is None:
        return None
    if kind is not EntryKind.FILE:
        raise ManifestError(f"{origin}: only a file entry has a content digest")
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ManifestError(f"{origin}: {value!r} is not a lowercase SHA-256")
    return value


# meiva:note
# A tree-relative key: "." for the root, otherwise a relative POSIX path with no empty, "." or
# ".." segment, so a manifest cannot name anything outside the tree it describes.
def _check_key(key: object, *, origin: str) -> str:
    if not isinstance(key, str):
        raise ManifestError(f"{origin}: entry keys are strings")
    if key == ".":
        return key
    parts = key.split("/")
    if key.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise ManifestError(f"{origin}: {key!r} is not a tree-relative path")
    return key


# meiva:note
# Every entry of one tree, keyed by tree-relative path, and the absolute path the tree was sealed
# at: a release's virtualenv embeds that path, so a tree found anywhere else is a finding.
@dataclass(frozen=True)
class Manifest:
    root: str
    entries: Mapping[str, Entry]

    def to_json(self) -> str:
        body = {
            "schema": MANIFEST_SCHEMA,
            "root": self.root,
            "entries": {key: self.entries[key].as_row() for key in sorted(self.entries)},
        }
        return json.dumps(body, separators=(",", ":")) + "\n"

    @classmethod
    def parse(cls, raw: str, *, origin: str) -> Manifest:
        try:
            data = object_of(json.loads, raw)
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{origin}: not JSON: {exc}") from exc
        if not _is_str_dict(data) or frozenset(data) != _MANIFEST_FIELDS:
            raise ManifestError(f"{origin}: a manifest has exactly {sorted(_MANIFEST_FIELDS)}")
        if data["schema"] != MANIFEST_SCHEMA:
            raise ManifestError(f"{origin}: schema {data['schema']!r} is not {MANIFEST_SCHEMA}")
        root, rows = data["root"], data["entries"]
        if not isinstance(root, str) or not root.startswith("/"):
            raise ManifestError(f"{origin}: root must be an absolute path")
        if not _is_object_dict(rows):
            raise ManifestError(f"{origin}: entries must be an object")
        entries = {
            _check_key(key, origin=origin): Entry.from_row(row, origin=f"{origin}: {key}")
            for key, row in rows.items()
        }
        if "." not in entries or entries["."].kind is not EntryKind.DIR:
            raise ManifestError(f"{origin}: the root entry '.' must be a directory")
        return cls(root=root, entries=entries)


# meiva:note
# Every way an observed tree differs from its manifest, one line per path, sorted: a moved root,
# missing and added paths, and changed stat fields (plus the content digest when `content` is
# set). Empty means the tree is the one that was sealed.
def differences(
    sealed: Manifest, root: str, observed: Mapping[str, Entry], *, content: bool
) -> tuple[str, ...]:
    findings: list[str] = []
    if root != sealed.root:
        findings.append(f"moved: sealed at {sealed.root}, found at {root}")
    findings += [f"missing: {path}" for path in sorted(sealed.entries.keys() - observed.keys())]
    findings += [f"added: {path}" for path in sorted(observed.keys() - sealed.entries.keys())]
    for path in sorted(sealed.entries.keys() & observed.keys()):
        before, after = sealed.entries[path], observed[path]
        if _facts(before, content=content) == _facts(after, content=content):
            continue
        changed = _changed_fields(before, after, content=content)
        findings.append(f"changed: {path} ({', '.join(changed)})")
    return tuple(findings)


# meiva:note
# The compared fields as one tuple, so an unchanged path (nearly all of them) costs one tuple
# comparison; the names of what changed are worked out only for a path that did.
def _facts(entry: Entry, *, content: bool) -> tuple[object, ...]:
    facts = (
        entry.kind,
        entry.mode,
        entry.size,
        entry.inode,
        entry.mtime_ns,
        entry.ctime_ns,
        entry.target,
    )
    return (*facts, entry.sha256) if content else facts


def _changed_fields(sealed: Entry, seen: Entry, *, content: bool) -> list[str]:
    fields = [
        name
        for name, was, now in zip(
            _STAT_FIELDS, _facts(sealed, content=False), _facts(seen, content=False), strict=True
        )
        if was != now
    ]
    if content and sealed.sha256 != seen.sha256:
        fields.append("sha256")
    return fields


# meiva:note
# The first `limit` findings and a count of the rest: a tree someone ran `uv sync` into differs in
# thousands of paths, and the first few name the cause.
def report(findings: Sequence[str], *, limit: int = MAX_REPORTED) -> tuple[str, ...]:
    if len(findings) <= limit:
        return tuple(findings)
    return (*findings[:limit], f"... and {len(findings) - limit} more")


# meiva:note
# The manifest sits beside its release, outside the tree, so it can record the stat facts the
# sealing itself produced.
def manifest_path(release: Path) -> Path:
    return release.parent / f"{release.name}.manifest.json"
