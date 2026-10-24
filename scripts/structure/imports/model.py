# meiva:link scripts/structure/imports/README.md

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

__all__ = ["Evidence", "Fact", "FactKind", "FileExecution", "ResourceRead", "Unknown"]


class FactKind(StrEnum):
    IMPORT = "import"
    DYNAMIC_IMPORT = "dynamic-import"
    EMBEDDED_IMPORT = "embedded-import"
    PYTHON_MODULE = "python-module"
    RESOURCE = "resource"


@dataclass(frozen=True)
class Fact:
    line: int
    kind: FactKind
    target: str
    names: tuple[str, ...] = ()
    via: str = ""
    member: str | None = None

    def __post_init__(self) -> None:
        if self.member is not None and (
            self.kind is not FactKind.RESOURCE
            or self.member in {"", ".", ".."}
            or "/" in self.member
            or "\\" in self.member
        ):
            raise ValueError("A resource member must be one literal filename on a RESOURCE fact")

    def matches_member(self, path: str) -> bool:
        # meiva:link scripts/structure/imports/README.md
        if self.member is None:
            return False
        root, changed = PurePosixPath(self.target).parts, PurePosixPath(path).parts
        return (
            changed == root
            or changed[:-1] == root
            or (
                len(changed) == len(root) + 2
                and changed[: len(root)] == root
                and changed[-1] == self.member
            )
        )


@dataclass(frozen=True)
class Unknown:
    path: str
    line: int
    expression: str
    reason: str
    kind: FactKind = FactKind.DYNAMIC_IMPORT


@dataclass(frozen=True)
class ResourceRead:
    # meiva:link scripts/structure/imports/README.md

    line: int
    target: str
    operation: str


@dataclass(frozen=True)
class FileExecution:
    # meiva:link scripts/structure/imports/README.md

    line: int
    target: str
    name: str


@dataclass(frozen=True)
class Evidence:
    records: tuple[Fact, ...]
    unknown: tuple[Unknown, ...]
    resource_reads: tuple[ResourceRead, ...] = ()
    file_executions: tuple[FileExecution, ...] = ()
