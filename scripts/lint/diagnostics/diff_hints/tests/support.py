from __future__ import annotations

from scripts.lint.diagnostics.diff_hints.engine import analyze
from scripts.lint.diagnostics.diff_hints.model import FileChange, HintId, Roles, Site
from scripts.lint.diagnostics.diff_hints.scope import parse_unit

ROLES = Roles(
    production=frozenset({"agent", "base", "cli", "gateway", "meiva", "services"}),
    tests=frozenset({"tests"}),
    first_party=frozenset({"agent", "base", "cli", "gateway", "meiva", "services", "scripts"}),
)


# meiva:note
# Every line of the source is an added line unless the test names the added lines.
def sites(
    source: str, path: str = "cli/x.py", *, added: frozenset[int] | None = None
) -> list[Site]:
    text = source.lstrip("\n")
    lines = added if added is not None else frozenset(range(1, len(text.splitlines()) + 1))
    change = FileChange(path, lines, (), (), deleted=False)
    return analyze((change,), {path: text}, ROLES)


def hints(source: str, path: str = "cli/x.py") -> list[HintId]:
    return [site.hint for site in sites(source, path)]


def unit_of(source: str, path: str = "cli/x.py") -> object:
    text = source.lstrip("\n")
    return parse_unit(path, text, frozenset(range(1, len(text.splitlines()) + 1)), ROLES)
