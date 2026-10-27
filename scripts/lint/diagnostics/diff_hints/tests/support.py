from __future__ import annotations

from meizel.graph.model import AmbientKind
from scripts.lint.diagnostics.diff_hints.engine import analyze
from scripts.lint.diagnostics.diff_hints.model import (
    FileChange,
    HintId,
    Importers,
    NewRoot,
    Roles,
    Site,
)
from scripts.lint.diagnostics.diff_hints.scope import parse_unit

ROLES = Roles(
    production=frozenset({"agent", "base", "cli", "gateway", "meiva", "services"}),
    tests=frozenset({"tests"}),
    first_party=frozenset({"agent", "base", "cli", "gateway", "meiva", "services", "scripts"}),
)


# meiva:note
# Every line of the source is an added line unless the test names the added lines.
def sites(
    source: str,
    path: str = "cli/x.py",
    *,
    added: frozenset[int] | None = None,
    roots: frozenset[AmbientKind] = frozenset(),
    importers: int = 0,
    onward: int = 0,
    new_roots: tuple[NewRoot, ...] = (),
) -> list[Site]:
    text = source.lstrip("\n")
    lines = added if added is not None else frozenset(range(1, len(text.splitlines()) + 1))
    change = FileChange(path, lines, (), (), deleted=False)
    direct = tuple(f"cli/user{index}.py" for index in range(importers))
    through = ((direct[0], onward),) if direct and onward else ()
    return analyze(
        (change,),
        {path: text},
        ROLES,
        {path: roots},
        {path: Importers(direct, through)},
        new_roots,
    )


def hints(source: str, path: str = "cli/x.py") -> list[HintId]:
    return [site.hint for site in sites(source, path)]


def unit_of(source: str, path: str = "cli/x.py") -> object:
    text = source.lstrip("\n")
    return parse_unit(
        path,
        text,
        frozenset(range(1, len(text.splitlines()) + 1)),
        ROLES,
        frozenset(),
        Importers((), ()),
    )
