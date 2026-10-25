from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, TypeGuard


class GitSource(TypedDict):
    kind: Literal["git"]
    repo: str
    treeish: str


class ManifestEntry(TypedDict):
    id: str
    path: str
    source: GitSource
    sha256: str
    status: str
    notes: str


class RegistryManifest(TypedDict):
    version: int
    entries: list[ManifestEntry]


class OutcomeJson(TypedDict):
    id: str
    path: str
    ok: bool
    errors: list[str]


@dataclass(frozen=True)
class Outcome:
    entry_id: str
    path: str
    ok: bool
    errors: tuple[str, ...]

    def as_json(self) -> OutcomeJson:
        return {
            "id": self.entry_id,
            "path": self.path,
            "ok": self.ok,
            "errors": list(self.errors),
        }


# meiva:note
# The parsed command line. `root` is None when the operator named none, which means the home's
# source checkout.
@dataclass(frozen=True)
class CliArgs:
    root: Path | None
    manifest: Path
    json_output: bool


def _is_raw_dict(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def _is_manifest_entries(value: object) -> TypeGuard[list[ManifestEntry]]:
    return isinstance(value, list)


def validate_manifest(raw: object) -> RegistryManifest:
    if not _is_raw_dict(raw):
        raise TypeError("private-files manifest must be a JSON object")
    version = raw["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise ValueError(f"unsupported private-files manifest version: {version!r}")
    entries = raw["entries"]
    if not _is_manifest_entries(entries):
        raise TypeError("private-files manifest entries must be a list")
    return {"version": version, "entries": entries}


def relative_path(raw_path: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"private-files path must be relative and stay under root: {raw_path!r}")
    return path
