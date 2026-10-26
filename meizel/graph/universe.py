"""The node universe: tracked files and their directories, from `git ls-files`."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

from meizel.graph.model import MeizelError

__all__ = [
    "MANIFEST",
    "SOURCE_EXTENSIONS",
    "EditPlan",
    "GitRepo",
    "Module",
    "Universe",
    "is_private_module",
    "load_universe",
    "parent_of",
    "toplevel",
    "tracked_files",
]

MANIFEST = "meizel.toml"
SOURCE_EXTENSIONS = (".py", ".ts", ".tsx", ".mts", ".cts")
_is_file = Path.is_file
_mkdir = Path.mkdir
_read_file_bytes = Path.read_bytes
_rmdir = Path.rmdir
_unlink = Path.unlink
_write_file_bytes = Path.write_bytes


def _read_text(path: Path) -> str:
    return _read_file_bytes(path).decode("utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    _mkdir(path.parent, parents=True, exist_ok=True)
    _write_file_bytes(path, data)


def _write_text(path: Path, text: str) -> None:
    _write_bytes(path, text.encode("utf-8"))


def _mkdtemp(root: Path, prefix: str) -> Path:
    base = root.resolve().parent
    for idx in range(100_000):
        candidate = base / f".{prefix}{idx}"
        try:
            _mkdir(candidate, mode=0o700)
            return candidate
        except FileExistsError:
            continue
    raise MeizelError("unable to allocate temporary directory")


def _rmtree(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        base = Path(dirpath)
        for name in filenames:
            with suppress(OSError):
                _unlink(base / name)
        for name in dirnames:
            sub = base / name
            with suppress(OSError):
                if sub.is_symlink():
                    _unlink(sub)
                else:
                    _rmdir(sub)
    with suppress(OSError):
        _rmdir(root)


@dataclass(frozen=True, slots=True)
class EditPlan:
    writes: dict[Path, str] = field(default_factory=dict[Path, str])
    byte_writes: dict[Path, bytes] = field(default_factory=dict[Path, bytes])
    deletes: tuple[Path, ...] = ()
    dirs: tuple[Path, ...] = ()

    def apply(self) -> None:
        for directory in self.dirs:
            _mkdir(directory, parents=True, exist_ok=True)
        for path, text in self.writes.items():
            _write_text(path, text)
        for path, data in self.byte_writes.items():
            _write_bytes(path, data)
        for path in self.deletes:
            _unlink(path)


@dataclass(frozen=True, slots=True)
class GitRepo:
    root: Path

    def has_file(self, rel: str) -> bool:
        return _is_file(self.root / rel)

    def load_text(self, rel: str, *, default: str | None = None) -> str:
        full = self.root / rel
        if default is not None and not _is_file(full):
            return default
        return _read_text(full)

    def load_bytes(self, rel: str) -> bytes:
        return _read_file_bytes(self.root / rel)

    def run_text(self, *args: str) -> str:
        # meiva:note
        # Fixed git argv against the checkout root without invoking a shell.
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise MeizelError(f"git {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    def run_bytes(self, *args: str, stdin: bytes | None = None) -> bytes:
        # meiva:note
        # Fixed git argv for binary streams against the checkout root without invoking a shell.
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.root), *args],
            input=stdin,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise MeizelError(
                f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}"
            )
        return result.stdout

    def default_base(self) -> str:
        # meiva:note
        # Resolves the branch point with origin/main using a fixed git merge-base argv.
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.root), "merge-base", "HEAD", "origin/main"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else "HEAD"

    def show_optional_text(self, ref: str, path: str) -> str | None:
        # meiva:note
        # Reads a tracked blob from a git ref using a fixed git show argv after rejecting flag refs.
        if ref.startswith("-"):
            raise MeizelError(f"{ref}: not a git ref")
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.root), "show", f"{ref}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout if result.returncode == 0 else None

    def show_bytes(self, ref: str, path: str) -> bytes:
        return self.run_text("show", f"{ref}:{path}").encode()

    def stage_all(self) -> None:
        self.run_bytes("add", "-A")

    def grep_files(self, marker: str, suffixes: tuple[str, ...]) -> list[str]:
        # meiva:note
        # Lists tracked files containing a fixed marker via git grep without invoking a shell.
        done = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.root), "grep", "-l", "-F", marker, "--", "."],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode not in (0, 1):
            raise MeizelError(f"git grep failed: {done.stderr.strip()}")
        return sorted(
            p
            for p in done.stdout.splitlines()
            if p.endswith(suffixes) and not p.startswith("meizel/")
        )

    @contextmanager
    def checkout(self, ref: str) -> Generator[Path]:
        if ref.startswith("-"):
            raise MeizelError(f"{ref}: not a git ref")
        parent = _mkdtemp(self.root, "meizel-base-")
        target = parent / "base"
        try:
            self.run_bytes("worktree", "add", "--detach", str(target), ref)
            try:
                yield target
            finally:
                self.run_bytes("worktree", "remove", "--force", str(target))
        finally:
            _rmtree(parent)


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


def toplevel(cwd: Path) -> Path:
    result = subprocess.run(  # noqa: S603 - fixed argv
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MeizelError(f"not inside a git checkout: {result.stderr.strip()}")
    return Path(result.stdout.strip())


class Universe:
    """Tracked regular files, every directory above them, and module resolution."""

    DOOR_FILES: frozenset[str] = frozenset(
        {"__init__.py", "index.ts", "index.tsx", "index.mts", "index.cts"}
    )

    def __init__(self, root: Path, tracked: list[str]) -> None:
        self.root = root
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
        self._modules: dict[str, Module | None] = {}

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

    def resolve_module(self, dotted: str) -> Module | None:
        """Resolve a dotted name against the checkout, as `pythonpath = ["."]` would."""
        if dotted in self._modules:
            return self._modules[dotted]
        path = dotted.replace(".", "/")
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

    def has_file(self, rel: str) -> bool:
        return _is_file(self.root / rel)

    def load_text(self, rel: str) -> str:
        return _read_text(self.root / rel)

    def load_bytes(self, rel: str) -> bytes:
        return _read_file_bytes(self.root / rel)


def _is_regular(root: Path, rel: str) -> bool:
    full = root / rel
    return _is_file(full) and not full.is_symlink()


# meiva:note
# Fixed Git argv and NUL framing preserve tracked names containing whitespace or newlines.
def tracked_files(repo_root: Path, *, include_untracked: bool = False) -> list[str]:
    extra = ("--cached", "--others", "--exclude-standard") if include_untracked else ()
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo_root), "ls-files", "-z", *extra],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise MeizelError(f"git ls-files failed: {result.stderr.decode(errors='replace').strip()}")
    return [path.decode() for path in result.stdout.split(b"\0") if path]


def load_universe(root: Path) -> Universe:
    return Universe(root, sorted({path for path in tracked_files(root) if _is_regular(root, path)}))
