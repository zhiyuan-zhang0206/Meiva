# meiva:note
# The two structure ceilings and every judgment they make, as functions over values the shells have
# already read: a directory's entry names and kinds, a judged file's text, a Git path listing and the
# paths a target names. Nothing here opens a file, lists a directory, resolves a link or starts a
# process; which files and directories the ceilings judge, what they leave out and what the report
# says are decided here.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath

__all__ = [
    "DIRECTORY_CEILING",
    "HARD_CEILING",
    "ceiling_violations",
    "entry_candidate",
    "is_python_file",
    "judged_directories",
    "judged_path",
    "tracked_children",
]

HARD_CEILING = 800
DIRECTORY_CEILING = 20

_PYTHON_SUFFIX = ".py"
_MANIFEST = "meizel.toml"
# meiva:note
# The two names the file budget never enters: the interpreter's cache, and the migrations tree,
# whose files are append-only history rather than framework code.
_CACHE = "__pycache__"
_APPEND_ONLY = "migrations"
_IGNORED = frozenset({_CACHE, _APPEND_ONLY})
_FILE_CEILING_GUIDANCE = (
    "the file budget leaves room for cohesive logic behind a small interface; review "
    "responsibility boundaries, separate pure decisions from I/O orchestration, and "
    "use one of 3 clean refactoring shapes instead of mechanical shallow splits "
    "(*_part[0-9].py or naked *_helpers.py): "
    "(1) extract pure data contracts to contracts.py, "
    "(2) extract strict Pure Core module, "
    "(3) create a cohesive subpackage with a narrow meizel.toml exports door"
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
# The judged directories for the given targets, each with its direct entry count: a target's tracked
# descendants and its ancestors, the whole tree for the repository root itself, which never answers
# the cap on its own, and nothing for a target outside the repository.
def judged_directories(
    children: Mapping[str, set[str]], targets: Iterable[Path], repo_root: Path
) -> dict[str, int]:
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
    return {name: len(children[name]) for name in selected.intersection(children).difference({""})}


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
# What the file budget counts as source: a `.py` suffix, read the way `Path.suffix` reads it, so a
# directory or a notes file a target names straight is not read.
def is_python_file(name: str) -> bool:
    return PurePosixPath(name).suffix == _PYTHON_SUFFIX


# meiva:note
# Which of the two the file budget judges for one target and one structure directory, or None when
# they are disjoint or the judged path runs through a hidden name, the interpreter's cache or the
# migrations tree. The two are compared as the paths they are, so a target that holds the repository
# is judged as the structure directory inside it.
def judged_path(target: Path, scope: Path, repo_root: Path) -> Path | None:
    if target == scope or scope in target.parents:
        selected = target
    elif target in scope.parents:
        selected = scope
    else:
        return None
    try:
        relative = selected.relative_to(repo_root)
    except ValueError:
        return None
    if any(part.startswith(".") or part in _IGNORED for part in relative.parts):
        return None
    return selected


# meiva:note
# Both ceilings' report for one run, in the order the ceilings have always been printed in: a line
# per file over the hard ceiling, ordered by path *parts* (so `app/a/b.py` precedes `app/a.py`, as
# walking `Path` objects did), then a line per judged directory over the entry cap, by name.
def ceiling_violations(
    texts: Mapping[Path, str], repo_root: Path, judged: Mapping[str, int]
) -> list[str]:
    files = sorted(
        ((_label(path, repo_root), _line_count(text)) for path, text in texts.items()),
        key=lambda named: PurePosixPath(named[0]).parts,
    )
    return [
        *(
            f"{name}:{count}: file is {count} lines, over the {HARD_CEILING}-line hard ceiling"
            f" — {_FILE_CEILING_GUIDANCE}"
            for name, count in files
            if count > HARD_CEILING
        ),
        *(
            f"{name}: directory has {count} direct entries, over the {DIRECTORY_CEILING}-entry cap"
            f" — {_DIRECTORY_CEILING_GUIDANCE}"
            for name, count in sorted(judged.items())
            if count > DIRECTORY_CEILING
        ),
    ]


# meiva:note
# What counts as a line of a judged file: every line `str.splitlines` splits, with no trailing empty
# line counted for a file that ends at a newline.
def _line_count(text: str) -> int:
    return len(text.splitlines())


# meiva:note
# How a judged path is named in the report: repository-relative, or its absolute posix form when it
# sits outside the repository.
def _label(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
