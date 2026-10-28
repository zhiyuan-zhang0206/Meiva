# meiva:note
# The two structure ceilings and every judgment they make, as functions over values the shells have
# already read: a file's line count, a directory's entry names and kinds, a Git path listing and the
# paths a target names. Nothing here opens a file, lists a directory, resolves a link or starts a
# process; which files and directories the ceilings judge, what they leave out and what the report
# says are decided here.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Literal

__all__ = [
    "DIRECTORY_CEILING",
    "HARD_CEILING",
    "Side",
    "directory_violations",
    "entry_candidate",
    "file_violations",
    "judged_side",
    "selected_directories",
    "tracked_children",
]

HARD_CEILING = 800
DIRECTORY_CEILING = 20
Side = Literal["target", "scope"]

_MANIFEST = "meizel.toml"
# meiva:note
# The two names the file budget never enters: the interpreter's cache, and the migrations tree,
# whose files are append-only history rather than framework code.
_CACHE = "__pycache__"
_APPEND_ONLY = "migrations"
_IGNORED = frozenset({_CACHE, _APPEND_ONLY})
_FILE_CEILING_GUIDANCE = (
    "the file budget leaves room for cohesive logic behind a small interface; review "
    "responsibility boundaries, separate pure decisions from I/O orchestration, and move "
    "related implementation into a cohesive subpackage with a narrow meizel.toml exports "
    "door; avoid mechanical _part2 or _helpers splits"
)
_DIRECTORY_CEILING_GUIDANCE = (
    "the entry cap bounds the concepts at one directory, not total project size; directory "
    "depth is unrestricted, so group related behavior into cohesive subpackages with narrow "
    "meizel.toml exports doors"
)


# meiva:note
# Every directory's direct children from Git's NUL-delimited tracked paths. A `meizel.toml`
# manifest describes its directory and is not one of its entries, and a listing that does not end
# at a NUL or holds a path Git could not have written is refused rather than half-read.
def tracked_children(listing: str) -> dict[str, set[str]]:
    if listing and not listing.endswith("\0"):
        raise ValueError("tracked Git paths are not NUL-terminated")
    children: dict[str, set[str]] = {}
    for name in listing.split("\0")[:-1]:
        path = PurePosixPath(name)
        if not path.parts or path.is_absolute() or ".." in path.parts or path.as_posix() != name:
            raise ValueError(f"invalid tracked Git path: {name!r}")
        if path.name == _MANIFEST:
            continue
        for depth, child in enumerate(path.parts):
            directory = "/".join(path.parts[:depth])
            children.setdefault(directory, set()).add(child)
    return children


# meiva:note
# The judged directories for the given targets: each target's tracked descendants and its
# ancestors, and the whole tree for the repository root itself, which never answers the cap on its
# own. A target outside the repository is not judged at all.
def selected_directories(
    children: Mapping[str, set[str]], targets: Iterable[Path], repo_root: Path
) -> set[str]:
    selected: set[str] = set()
    for target in targets:
        try:
            relative = target.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if relative == ".":
            selected.update(children)
            continue
        selected.update(
            directory
            for directory in children
            if directory == relative or directory.startswith(f"{relative}/")
        )
        parts = PurePosixPath(relative).parts
        selected.update("/".join(parts[:depth]) for depth in range(1, len(parts)))
    return selected.intersection(children).difference({""})


# meiva:note
# Whether one directory entry is a candidate of the file budget: a link is not entered, a hidden
# name and the interpreter's cache are not source, and the migrations tree is append-only history
# rather than framework code. A *file* named migrations is not that tree, so the entry's kind
# decides.
def entry_candidate(name: str, *, is_symlink: bool, is_directory: bool) -> bool:
    if is_symlink or name.startswith(".") or name == _CACHE:
        return False
    return not (name == _APPEND_ONLY and is_directory)


# meiva:note
# Which of the two the file budget judges for one target and one structure directory, or None when
# they are disjoint or the judged path runs through a hidden name, the interpreter's cache or the
# migrations tree: the file budget only reads what it would walk anyway. The two are the target and
# the scope, not their union, so a target deep under one scope is judged as itself.
def judged_side(target: Path, scope: Path, repo_root: Path) -> Side | None:
    try:
        relative_target = target.relative_to(repo_root)
        relative_scope = scope.relative_to(repo_root)
    except ValueError:
        return None
    side: Side
    if relative_target == relative_scope or relative_scope in relative_target.parents:
        side, judged = "target", relative_target
    elif relative_target in relative_scope.parents:
        side, judged = "scope", relative_scope
    else:
        return None
    if any(part.startswith(".") or part in _IGNORED for part in judged.parts):
        return None
    return side


# meiva:note
# A line per file over the hard ceiling, given each judged file's repository-relative path and its
# line count, in path order.
def file_violations(lines: Mapping[str, int]) -> list[str]:
    return [
        f"{name}:{count}: file is {count} lines, over the {HARD_CEILING}-line hard ceiling"
        f" — {_FILE_CEILING_GUIDANCE}"
        for name, count in sorted(lines.items())
        if count > HARD_CEILING
    ]


# meiva:note
# A line per directory over the entry cap, given each judged directory's repository-relative path
# and its direct entry count, in path order.
def directory_violations(counts: Mapping[str, int]) -> list[str]:
    return [
        f"{name}: directory has {count} direct entries, over the {DIRECTORY_CEILING}-entry cap"
        f" — {_DIRECTORY_CEILING_GUIDANCE}"
        for name, count in sorted(counts.items())
        if count > DIRECTORY_CEILING
    ]
