from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, TypedDict, cast


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


@dataclass(frozen=True)
class CliArgs:
    root: Path
    manifest: Path
    json_output: bool


def validate_manifest(raw: object) -> RegistryManifest:
    if not isinstance(raw, dict):
        raise TypeError("private-files manifest must be a JSON object")
    manifest = cast(RegistryManifest, raw)
    if manifest["version"] != 1:
        raise ValueError(f"unsupported private-files manifest version: {manifest['version']!r}")
    if not isinstance(manifest["entries"], list):
        raise TypeError("private-files manifest entries must be a list")
    return manifest


def relative_path(raw_path: str) -> PurePosixPath:
    path = PurePosixPath(raw_path)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"private-files path must be relative and stay under root: {raw_path!r}")
    return path
