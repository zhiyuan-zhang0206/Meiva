"""First-party reference facts and syntax-derived repository-root path evidence."""

from __future__ import annotations

import ast
from dataclasses import dataclass

from scripts.structure import placement
from scripts.structure.imports import executed, facts

__all__ = [
    "SubjectLCA",
    "subject_lca",
]


@dataclass(frozen=True)
class SubjectLCA:
    """A complete Python subject directory proof (empty string is root), or its gaps."""

    directory: str | None
    modules: tuple[str, ...]
    unknown: tuple[executed.Unresolved, ...]


def subject_lca(tree: ast.AST, rel: str, index: object) -> SubjectLCA:
    """Resolve a directory LCA without import direction or all-patch fallback.

    Shared facts are the only parser. Test support and resources cannot prove
    Python subjects; all unknown inputs still prevent certification.
    """
    if not isinstance(index, placement.ModuleIndex):
        raise TypeError("subject LCA requires the shared module index")
    evidence = facts.collect(tree, rel, index, tops=placement.CODE_TOPS)
    refs: list[placement.Ref] = []
    for fact in evidence.records:
        if fact.kind == facts.FactKind.RESOURCE or "tests" in fact.target.split("."):
            continue
        unit = placement.unit_of(fact.target)
        if unit is None:
            continue
        kind = "string-target" if fact.kind == facts.FactKind.DYNAMIC_IMPORT else "import"
        if fact.kind in {facts.FactKind.EMBEDDED_IMPORT, facts.FactKind.PYTHON_MODULE}:
            kind = "embedded-import"
        refs.append(placement.Ref(fact.line, kind, fact.target, unit, fact.via, fact.names))
    pruned = placement.without_patch_evidence(list(ast.walk(tree)), refs)
    modules = tuple(sorted({ref.module for ref in pruned}))
    directory = (
        placement.common_dir([index.dir_of(module) for module in modules])
        if modules and not evidence.unknown
        else None
    )
    gaps = tuple(executed.Unresolved(u.path, u.line, u.reason) for u in evidence.unknown)
    return SubjectLCA(directory, modules, gaps)
