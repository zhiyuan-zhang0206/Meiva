# meiva:note
# The node universe: tracked files, their directories, and module resolution.
#
# Data only. `meizel.checkout` builds one from a checkout, runs git for it, and reads the files the
# universe names; nothing here touches the filesystem.
# meiva:link meizel/docs/checker/purity.meiva.okf.md

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from meizel.graph.python_layout import StandaloneProject

__all__ = [
    "MANIFEST",
    "SOURCE_EXTENSIONS",
    "Checkout",
    "EditPlan",
    "Module",
    "Universe",
    "is_private_module",
    "parent_of",
]

MANIFEST = "meizel.toml"
SOURCE_EXTENSIONS = (".py", ".ts", ".tsx", ".mts", ".cts")


# meiva:note
# What a command decided to write, delete or create; `meizel.checkout.apply_plan` does it.
@dataclass(frozen=True, slots=True)
class EditPlan:
    writes: dict[Path, str] = field(default_factory=dict[Path, str])
    byte_writes: dict[Path, bytes] = field(default_factory=dict[Path, bytes])
    deletes: tuple[Path, ...] = ()
    dirs: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class Module:
    """A first-party module: its source file, or None for a namespace directory."""

    file: str | None
    path: str


_DUNDER_MODULE = re.compile(r"__\w+__(\.py)?")


def is_private_module(child: str) -> bool:
    """A module or package whose name starts with an underscore is private to its directory.

    A dunder name (`__init__.py`, `__main__.py`) is the language's own protocol, not a private one.
    """
    return child.startswith("_") and _DUNDER_MODULE.fullmatch(child) is None


def parent_of(path: str) -> str:
    return path.rpartition("/")[0]


# meiva:note
# The checkout a decision layer works on: the root it reads and the files under it. The decision
# modules of `meizel/contracts` take this handle and never build one, so what they decide stays
# separable from the git and file reads that answer it; `meizel.checkout.GitRepo` is the one
# implementation, and it is the shell that runs git for the universe.
class Checkout(Protocol):
    @property
    def root(self) -> Path: ...

    def has_file(self, rel: str) -> bool: ...

    def load_text(self, rel: str, *, default: str | None = None) -> str: ...

    def load_bytes(self, rel: str) -> bytes: ...

    def run_text(self, *args: str) -> str: ...

    def run_bytes(self, *args: str, stdin: bytes | None = None) -> bytes: ...

    def default_base(self) -> str: ...

    def show_optional_text(self, ref: str, path: str) -> str | None: ...

    def show_bytes(self, ref: str, path: str) -> bytes: ...

    def stage_all(self) -> None: ...

    def apply_plan(self, plan: EditPlan) -> None: ...

    def grep_files(self, marker: str, suffixes: tuple[str, ...]) -> list[str]: ...

    def grep_words(self, words: Collection[str], suffix: str) -> list[str]: ...

    def untracked_files(self) -> tuple[str, ...]: ...

    def file_digests(self) -> dict[str, str]: ...

    def scanner_digest(self) -> str: ...

    def cache_get_many(self, keys: Collection[str]) -> dict[str, str]: ...

    def cache_put_many(self, entries: Mapping[str, str]) -> None: ...

    def memo_get(self, key: str) -> object | None: ...

    def memo_put(self, key: str, value: object) -> None: ...

    def universe(self) -> Universe: ...

    def checkout(self, ref: str) -> AbstractContextManager[Checkout]: ...


class Universe:
    """Tracked regular files, every directory above them, and module resolution."""

    DOOR_FILES: frozenset[str] = frozenset(
        {"__init__.py", "index.ts", "index.tsx", "index.mts", "index.cts"}
    )

    def __init__(
        self,
        repo: Checkout,
        tracked: list[str],
        *,
        projects: tuple[StandaloneProject, ...] = (),
        project: str | None = None,
    ) -> None:
        self.repo = repo
        self.root = repo.root
        self.files: frozenset[str] = frozenset(tracked)
        children: dict[str, set[str]] = {}
        for path in tracked:
            self._add_chain(children, path)
        self.dirs: frozenset[str] = frozenset(children)
        self.children: dict[str, tuple[str, ...]] = {
            directory: tuple(sorted(names)) for directory, names in children.items()
        }
        self.manifest_dirs: frozenset[str] = frozenset(
            parent_of(p) for p in tracked if p.rsplit("/", 1)[-1] == MANIFEST
        )
        self.python_files: tuple[str, ...] = tuple(sorted(p for p in tracked if p.endswith(".py")))
        self.typescript_files: tuple[str, ...] = tuple(
            sorted(
                p
                for p in tracked
                if p.endswith((".ts", ".tsx", ".mts", ".cts"))
                and not p.endswith((".d.ts", ".d.mts", ".d.cts"))
            )
        )
        self.roots: frozenset[str] = frozenset(
            name.removesuffix(".py") for name in self.children.get("", ())
        )
        self.standalone_projects = projects
        self._project = project
        active = next((item for item in projects if item.directory == project), None)
        self._python_aliases = dict(active.packages) if active is not None else {}
        self.roots = self.roots | frozenset(self._python_aliases)
        self._modules: dict[str, Module | None] = {}
        self._project_views: dict[str | None, Universe] = {project: self}
        self._digests: dict[str, str] | None = None

    @staticmethod
    def _add_chain(children: dict[str, set[str]], path: str) -> None:
        children.setdefault("", set())
        parts = path.split("/")
        for depth in range(len(parts)):
            directory = "/".join(parts[:depth])
            name = parts[depth]
            if depth == len(parts) - 1 and name == MANIFEST:
                continue
            children.setdefault(directory, set()).add(name)
            if depth < len(parts) - 1:
                children.setdefault("/".join(parts[: depth + 1]), set())

    def context(self, origin: str) -> Universe:
        owner = next(
            (
                item.directory
                for item in self.standalone_projects
                if origin == item.directory or origin.startswith(item.directory + "/")
            ),
            None,
        )
        if owner not in self._project_views:
            view = Universe(
                self.repo, sorted(self.files), projects=self.standalone_projects, project=owner
            )
            view._project_views = self._project_views
            self._project_views[owner] = view
        return self._project_views[owner]

    def context_for_module(self, dotted: str) -> Universe:
        head = dotted.split(".", 1)[0]
        for project in self.standalone_projects:
            if any(name == head for name, _ in project.packages):
                return self.context(project.directory)
        return self.context("")

    def canonical_name(self, dotted: str) -> str:
        head, separator, tail = dotted.partition(".")
        physical = self._python_aliases.get(head)
        return physical.replace("/", ".") + separator + tail if physical else dotted

    # meiva:note
    # Resolve root imports and explicit standalone wheel names to physical source.
    def resolve_module(self, dotted: str) -> Module | None:
        if dotted in self._modules:
            return self._modules[dotted]
        path = self.canonical_name(dotted).replace(".", "/")
        found: Module | None = None
        if f"{path}.py" in self.files:
            found = Module(f"{path}.py", f"{path}.py")
        elif f"{path}/__init__.py" in self.files:
            found = Module(f"{path}/__init__.py", path)
        elif path in self.dirs and path:
            found = Module(None, path)
        self._modules[dotted] = found
        return found

    def is_first_party_name(self, dotted: str) -> bool:
        return dotted.split(".", 1)[0] in self.roots

    def file_digests(self) -> dict[str, str]:
        if self._digests is None:
            self._digests = self.repo.file_digests()
        return self._digests

    def source_files(
        self, ts_scopes: tuple[str, ...] = (), ts_extensions: tuple[str, ...] = (".ts", ".tsx")
    ) -> tuple[str, ...]:
        if not ts_scopes:
            return self.python_files
        ts = tuple(
            p
            for p in self.typescript_files
            if p.endswith(ts_extensions) and any(p == s or p.startswith(f"{s}/") for s in ts_scopes)
        )
        return (*self.python_files, *ts)
