# meiva:note
# The report behind `where_used.py`: which group a reference belongs to and how it reads.
#
# `scan` turns the hits `references.file_hits` finds into one `Report`, `render` writes it as the
# grouped text a person reads and `as_json` as the complete structure scripts consume. Reading the
# checkout and printing are `where_used.py`; finding the references in one file is
# `analysis/references.py`.

from __future__ import annotations

import posixpath
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.audit.analysis.frozen import is_frozen
from scripts.audit.analysis.references import (
    Door,
    Hit,
    Repo,
    Target,
    build_matcher,
    file_hits,
    is_doc_file,
)

__all__ = [
    "GROUPS",
    "Report",
    "as_json",
    "group_of",
    "limit_error",
    "relative_target",
    "render",
    "scan",
]

GROUPS = ("imports", "strings", "tests", "docs", "structure", "history")
_TITLES = {
    "imports": "IMPORTS (production code)",
    "strings": "STRINGS (patch targets, entry points, registries, config)",
    "tests": "TESTS",
    "docs": "DOCS (markdown, docstrings, comments)",
    "structure": "STRUCTURE (baseline, pyproject, hooks, workflows, lint registries)",
    "history": "FROZEN HISTORY (not rewritten)",
}
# meiva:note
# How many directories the "more files" line breaks the rest into.
_OMITTED_DIRECTORIES = 6

_STRUCTURE_FILES = frozenset({"pyproject.toml", ".pre-commit-config.yaml"})
_STRUCTURE_PREFIXES = (
    "scripts/structure/",
    "scripts/lint/",
    "scripts/content_lint/",
    ".github/",
)
_IMPORT_KINDS = frozenset(
    {"import", "import in function", "wildcard import", "re-export", "attribute"}
)
_PROSE_KINDS = frozenset({"docstring", "comment"})
# meiva:note
# Not worth a bracketed label.
_PLAIN_KINDS = frozenset({"import", "mention", "reference"})


@dataclass(frozen=True)
class Report:
    # meiva:note
    # `hidden` counts the references from inside the target itself: they move with it.

    target: Target
    doors: tuple[Door, ...]
    groups: dict[str, list[Hit]]
    hidden: int


def _is_test(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        "tests" in parts[:-1]
        or name == "conftest.py"
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def group_of(path: str, kind: str) -> str:
    # meiva:note
    # The one group a hit belongs to: the file decides first, then the kind of hit.
    if is_frozen(path):
        return "history"
    if is_doc_file(path):
        return "docs"
    if _is_test(path):
        return "tests"
    if kind in _PROSE_KINDS:
        return "docs"
    if kind in _IMPORT_KINDS:
        return "imports"
    structure = path in _STRUCTURE_FILES or path.startswith(_STRUCTURE_PREFIXES)
    return "structure" if structure else "strings"


def _is_internal(target: Target, path: str, group: str) -> bool:
    # meiva:note
    # A reference from the target itself: it moves with the target.
    if path == target.path:
        return True
    inside = target.is_dir and path.startswith(target.path + "/")
    return inside and path.endswith(".py") and group != "tests"


def _named_for(path: str, stem: str) -> bool:
    # meiva:note
    # `test_<module>.py` or `test_<module>_<what>.py`, but not `test_<module>x.py`.
    name = posixpath.basename(path)
    return name.startswith(stem) and name[len(stem) :].startswith(("_", "."))


def _name_matches(repo: Repo, target: Target, present: set[str]) -> list[Hit]:
    # meiva:note
    # Tests named for the module that no hit led to: where its own tests would live.
    if target.kind not in ("module", "symbol") or target.path.endswith("__init__.py"):
        # meiva:note
        # A package's own name is too generic to say which tests are its own.
        return []
    stem = f"test_{target.module.rsplit('.', 1)[-1]}"
    return [
        Hit(path, 0, "name match", "")
        for path in sorted(repo.texts)
        if path.endswith(".py")
        and path not in present
        and _is_test(path)
        and _named_for(path, stem)
    ]


def scan(repo: Repo, target: Target) -> Report:
    # meiva:note
    # Every reference to `target`, grouped.
    doors = repo.find_doors(target.module, target.name) if target.name else []
    matcher = build_matcher(repo, target, doors)
    found: dict[tuple[str, int], Hit] = {}
    for path, text in repo.texts.items():
        for hit in file_hits(path, text, matcher, repo):
            found.setdefault((hit.path, hit.line), hit)
    groups: dict[str, list[Hit]] = {group: [] for group in GROUPS}
    hidden = 0
    for hit in sorted(found.values(), key=lambda h: (h.path, h.line)):
        group = group_of(hit.path, hit.kind)
        if _is_internal(target, hit.path, group):
            hidden += 1
        else:
            groups[group].append(hit)
    groups["tests"].extend(_name_matches(repo, target, {h.path for h in groups["tests"]}))
    return Report(target, tuple(doors), groups, hidden)


def _by_file(hits: Sequence[Hit]) -> dict[str, list[Hit]]:
    files: dict[str, list[Hit]] = {}
    for hit in hits:
        files.setdefault(hit.path, []).append(hit)
    return files


def _entry(path: str, hits: list[Hit]) -> str:
    first = hits[0]
    if first.kind == "name match":
        return f"  {path}  [named for the module, no reference]"
    label = "" if first.kind in _PLAIN_KINDS else f"  [{first.kind}]"
    more = f"  (+{len(hits) - 1} at {', '.join(str(h.line) for h in hits[1:])})" if hits[1:] else ""
    return f"  {path}:{first.line}{label}  {first.text}{more}"


# meiva:note
# The line that ends a group a `--limit` cut: `... N MORE FILES HIDDEN by --limit (omit it to list
# every file): dir N, dir N`.
def _omitted(paths: Sequence[str]) -> str:
    top = Counter(path.split("/")[0] for path in paths).most_common(_OMITTED_DIRECTORIES)
    where = ", ".join(f"{directory} {count}" for directory, count in top)
    return f"  ... {len(paths)} MORE FILES HIDDEN by --limit (omit it to list every file): {where}"


def _group_lines(group: str, hits: Sequence[Hit], limit: int | None) -> list[str]:
    files = _by_file(hits)
    if not files:
        return [f"{_TITLES[group]}: none"]
    header = f"{_TITLES[group]}: {len(files)} files, {len(hits)} sites"
    if group == "tests":
        beside = sum(1 for path in files if not path.startswith("tests/"))
        header += f" ({beside} beside the code, {len(files) - beside} in top-level tests/)"
    entries = list(files.items())
    shown = entries if limit is None else entries[:limit]
    lines = [header, *(_entry(path, found) for path, found in shown)]
    if len(shown) < len(entries):
        lines.append(_omitted([path for path, _ in entries[len(shown) :]]))
    return lines


def _describe(target: Target) -> str:
    if target.kind == "symbol":
        return f"symbol, defined at {target.defined_at}"
    return f"{target.kind} {target.path}"


def render(report: Report, limit: int | None) -> str:
    # meiva:note
    # The report as text; `limit` caps the files shown per group (None shows all).
    target = report.target
    lines = [f"where-used {target.raw}  [{_describe(target)}]"]
    if report.doors:
        doors = ", ".join(f"{d.module}:{d.name} ({d.where})" for d in report.doors)
        lines.append(f"also importable through: {doors}")
    counts = " | ".join(f"{group} {len(_by_file(report.groups[group]))}" for group in GROUPS)
    lines.append(f"files per group: {counts}")
    for group in GROUPS:
        lines.extend(_group_lines(group, report.groups[group], limit))
    hidden = (
        f"{report.hidden} references from inside the target are hidden. " if report.hidden else ""
    )
    lines.append(f"{hidden}Static references only: runtime-built names are not seen.")
    return "\n".join(lines)


def as_json(report: Report) -> dict[str, object]:
    # meiva:note
    # The complete report (no per-group cap) for scripts.
    target = report.target
    return {
        "target": {
            "raw": target.raw,
            "kind": target.kind,
            "path": target.path,
            "module": target.module or None,
            "name": target.name or None,
            "defined_at": target.defined_at or None,
            "importable_through": [asdict(door) for door in report.doors],
        },
        "groups": {
            group: {
                "files": len(_by_file(hits)),
                "sites": len(hits),
                "hits": [asdict(hit) for hit in hits],
            }
            for group, hits in report.groups.items()
        },
        "hidden_internal": report.hidden,
    }


# meiva:note
# A target as the report names it: an absolute path inside the checkout becomes relative to it, a
# `:name` suffix is kept. Resolving the path is the shell's.
def relative_target(raw: str, resolved: Path | None, repo_root: Path) -> str:
    spec, sep, rest = raw.partition(":")
    if resolved is not None and resolved.is_relative_to(repo_root):
        spec = resolved.relative_to(repo_root).as_posix()
    return spec + sep + rest


# meiva:note
# Why `--limit N` is unusable, or None when it is: a report lists at least one file per group.
def limit_error(limit: int | None) -> str | None:
    if limit is not None and limit < 1:
        return "--limit must be at least 1"
    return None
