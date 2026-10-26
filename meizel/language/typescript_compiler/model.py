from __future__ import annotations

from dataclasses import dataclass

from meizel.graph.model import (
    Acquisition,
    AnyCollaboratorSite,
    EnumSubsetUse,
    Fact,
    Patch,
    Problem,
    PurityItem,
    Restricted,
    TypeCastSite,
)


@dataclass(frozen=True, slots=True)
class TsScan:
    path: str
    facts: tuple[Fact, ...]
    external: frozenset[str]
    patches: tuple[Patch, ...]
    purity: tuple[PurityItem, ...]
    problems: tuple[Problem, ...]
    exports_all: frozenset[str] | None
    defined: frozenset[str]
    ambient: tuple[Acquisition, ...]
    type_casts: tuple[TypeCastSite, ...]
    any_collaborators: tuple[AnyCollaboratorSite, ...]
    enum_subsets: tuple[EnumSubsetUse, ...]
    symbols: tuple[tuple[str, int, int], ...]
    restricted: tuple[Restricted, ...]
