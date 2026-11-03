# meiva:note
# The shape of the ownership ledger the external skill host persists, and of the manifests and
# transactions inside it.
#
# These are plain dicts on disk (JSON) and stay plain dicts in memory, because the host mutates
# them in place and writes them back after each step. `TypedDict`s name their keys and values, so a
# reader holds `str` and `Literal` states instead of `Any`. A manifest entry is a file (with a
# digest) or a directory (without one), told apart by `kind`.

from __future__ import annotations

from typing import Literal, TypedDict

__all__ = [
    "Claim",
    "CleanupItem",
    "DirectoryEntry",
    "FileEntry",
    "Installed",
    "Ledger",
    "ManifestEntry",
    "Transaction",
]


class FileEntry(TypedDict):
    kind: Literal["file"]
    mode: int
    path: str
    sha256: str


class DirectoryEntry(TypedDict):
    kind: Literal["directory"]
    mode: int
    path: str


type ManifestEntry = FileEntry | DirectoryEntry


class Claim(TypedDict):
    path: str
    quarantine: str
    state: Literal["source", "claiming", "quarantine", "retained"]


class CleanupItem(TypedDict):
    file_claims: list[Claim]
    kind: Literal["prepared", "stage", "previous"]
    location: Literal["source", "claiming", "quarantine", "retained"]
    manifest: list[ManifestEntry]
    path_generation_id: str


class Installed(TypedDict):
    digest: str
    generation_id: str
    manifest: list[ManifestEntry]
    source_digest: str


class Transaction(TypedDict):
    claim_state: Literal["idle", "claiming", "claimed"]
    expected_digest: str
    expected_manifest: list[ManifestEntry]
    generation_id: str
    source_digest: str
    stage_state: Literal["preparing", "publishing", "published"]


class Ledger(TypedDict):
    client: str
    format: int
    garbage: list[CleanupItem]
    installation_id: str
    installed: Installed | None
    retained: list[CleanupItem]
    transaction: Transaction | None
