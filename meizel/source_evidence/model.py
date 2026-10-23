from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Identity:
    path: str
    sha256: str


@dataclass(frozen=True)
class Declaration:
    dependency: str
    line: int


@dataclass(frozen=True)
class Observation:
    line: int
    end_line: int
    binding: str
    properties: tuple[str, ...]
    kind: Literal["module-allocation", "unknown"]
    constructor: Literal["Map", "Set", "unknown"]
    declarations: tuple[Declaration, ...]
    frozen_surface: bool
    freeze_receiver: tuple[Declaration, ...]
    freeze_callee: tuple[Declaration, ...]
    reason: str

    def __post_init__(self) -> None:
        if self.line < 1 or self.end_line < self.line:
            raise ValueError("invalid observation range")
        if self.kind == "module-allocation":
            if self.constructor == "unknown" or not self.binding or not self.declarations:
                raise ValueError("module allocation requires binding and constructor provenance")
        elif self.constructor != "unknown" or self.declarations or self.frozen_surface:
            raise ValueError("unknown observation cannot assert constructor provenance")
        self._check_freeze()

    def _check_freeze(self) -> None:
        if self.frozen_surface != bool(self.freeze_receiver and self.freeze_callee):
            raise ValueError("shallow freeze requires receiver and callee provenance")
        if not self.frozen_surface and (self.freeze_receiver or self.freeze_callee):
            raise ValueError("unexpected freeze provenance")


@dataclass(frozen=True)
class Source:
    path: str
    sha256: str
    status: Literal["observed", "unavailable"]
    reason: str
    observations: tuple[Observation, ...]

    def __post_init__(self) -> None:
        if self.status == "unavailable" and self.observations:
            raise ValueError("unavailable source cannot contain observations")


@dataclass(frozen=True)
class Index:
    version: Literal[1]
    kind: Literal["meizel-ts-holders"]
    root: str
    compiler_version: str
    dependencies: tuple[Identity, ...]
    sources: tuple[Source, ...]


@dataclass(frozen=True)
class TsReport:
    status: Literal["not-requested", "not-applicable", "unavailable", "observed"]
    sources: tuple[Source, ...]

    def __post_init__(self) -> None:
        if self.status in ("not-requested", "not-applicable"):
            if self.sources:
                raise ValueError("inactive report cannot contain sources")
        elif not self.sources:
            raise ValueError("active report requires source receipts")
        elif self.status == "unavailable" and any(s.status != "unavailable" for s in self.sources):
            raise ValueError("unavailable report cannot contain observed sources")
        elif self.status == "observed" and all(s.status != "observed" for s in self.sources):
            raise ValueError("observed report requires an observed source receipt")
