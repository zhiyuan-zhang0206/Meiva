# meiva:note
# The checkout this tool reads: git, the text of its files, and the plans applied to it.
#
# The effect shell of `meizel.graph.universe`: the universe holds what a checkout contains, this
# module is the only place that runs git against it, reads a file from it, or writes one back.
# `mode = "none"` in `meizel/meizel.toml` records that split.
# meiva:link meizel/docs/checker/purity.meiva.okf.md

from __future__ import annotations

import errno
import os
import stat
import subprocess
from collections.abc import Callable, Collection, Generator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from meizel.graph import project_metadata_paths, standalone_projects
from meizel.graph.model import MeizelError
from meizel.graph.universe import MANIFEST, EditPlan, Universe
from meizel.roles import parse_roles

__all__ = [
    "GitRepo",
    "apply_plan",
    "has_file",
    "load_universe",
    "read_bytes",
    "read_roles",
    "read_text",
    "roles_at",
    "toplevel",
    "tracked_files",
]


# meiva:note
# The roles a checkout's root manifest declares: the parse is the model's (`meizel.roles`),
# the read is this shell's, so a module that only decides over roles never runs git.
def read_roles(repo: GitRepo) -> Mapping[str, tuple[str, ...]]:
    path = repo.root / MANIFEST
    try:
        text = repo.load_text(MANIFEST)
    except OSError as error:
        raise MeizelError(f"{path}: cannot read the roles: {error}") from error
    return parse_roles(text, label=str(path))


# meiva:note
# The roles of a repository root, for an entry point that has no checkout yet: the script or
# command that runs on this repository is the one place allowed to build one.
def roles_at(root: Path) -> Mapping[str, tuple[str, ...]]:
    return read_roles(GitRepo(root))


# meiva:note
# The UTF-8 text of `rel` under `root`.
def read_text(root: Path, rel: str) -> str:
    return (root / rel).read_bytes().decode("utf-8")


# meiva:note
# The bytes of `rel` under `root`.
def read_bytes(root: Path, rel: str) -> bytes:
    return (root / rel).read_bytes()


# meiva:note
# Whether `rel` under `root` is a regular file reachable by name.
def has_file(root: Path, rel: str) -> bool:
    return (root / rel).is_file()


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_text(path: Path, text: str) -> None:
    _write_bytes(path, text.encode("utf-8"))


def _mkdtemp(root: Path, prefix: str) -> Path:
    base = root.resolve().parent
    for idx in range(100_000):
        candidate = base / f".{prefix}{idx}"
        try:
            candidate.mkdir(mode=0o700)
            return candidate
        except FileExistsError:
            continue
    raise MeizelError("unable to allocate temporary directory")


def _rmtree(root: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        base = Path(dirpath)
        for name in filenames:
            with suppress(OSError):
                (base / name).unlink()
        for name in dirnames:
            sub = base / name
            with suppress(OSError):
                if sub.is_symlink():
                    sub.unlink()
                else:
                    sub.rmdir()
    with suppress(OSError):
        root.rmdir()


# meiva:note
# Carry out a plan a command built: create its directories, then write, then delete.
def apply_plan(plan: EditPlan) -> None:
    for directory in plan.dirs:
        directory.mkdir(parents=True, exist_ok=True)
    for path, text in plan.writes.items():
        _write_text(path, text)
    for path, data in plan.byte_writes.items():
        _write_bytes(path, data)
    for path in plan.deletes:
        path.unlink()


@dataclass(frozen=True, slots=True)
class GitRepo:
    root: Path

    def has_file(self, rel: str) -> bool:
        return has_file(self.root, rel)

    def load_text(self, rel: str, *, default: str | None = None) -> str:
        if default is not None and not has_file(self.root, rel):
            return default
        return read_text(self.root, rel)

    def load_bytes(self, rel: str) -> bytes:
        return read_bytes(self.root, rel)

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

    def apply_plan(self, plan: EditPlan) -> None:
        apply_plan(plan)

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

    def grep_words(self, words: Collection[str], suffix: str) -> list[str]:
        # meiva:note
        # Lists tracked files with the given suffix that contain any of the words as a whole word,
        # via git grep with a fixed argv and without invoking a shell.
        if not words:
            return []
        patterns = [arg for word in sorted(words) for arg in ("-e", word)]
        done = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.root), "grep", "-l", "-w", "-F", *patterns, "--", f"*{suffix}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if done.returncode not in (0, 1):
            raise MeizelError(f"git grep failed: {done.stderr.strip()}")
        return sorted(done.stdout.splitlines())

    # meiva:note
    # The file index of this checkout: the tracked regular files and the directories above them.
    # Built here, where the repository is read, so a caller holds the value instead of a path it
    # would rebuild the index from.
    def universe(self) -> Universe:
        return load_universe(self.root)

    @contextmanager
    def checkout(self, ref: str) -> Generator[GitRepo]:
        # meiva:note
        # The same seat over a detached checkout of `ref`, removed on exit: a caller compares two
        # trees by holding two seats, never by rebuilding one from a path it made.
        if ref.startswith("-"):
            raise MeizelError(f"{ref}: not a git ref")
        parent = _mkdtemp(self.root, "meizel-base-")
        target = parent / "base"
        try:
            self.run_bytes("worktree", "add", "--detach", str(target), ref)
            try:
                yield GitRepo(target)
            finally:
                self.run_bytes("worktree", "remove", "--force", str(target))
        finally:
            _rmtree(parent)


def toplevel(cwd: Path) -> Path:
    # meiva:note
    # Fixed git argv and no shell: the directive covers this call, whose arguments are literals.
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MeizelError(f"not inside a git checkout: {result.stderr.strip()}")
    return Path(result.stdout.strip())


# meiva:note
# A path is regular when it is a file and no component between it and the root is a
# link: git tracks a path, not how it is reached, so a directory swapped for a link
# keeps its tracked children listed. `git ls-files` names are relative, so one `lstat`
# answers the file half (a link is not a regular file) and each directory's answer is
# remembered for one listing — the same rule as before without a `Path` per component
# per file (9.1k names: 0.63s -> 0.05s). The join is textual, and `git ls-files` emits
# only relative names, so this walk claims nothing about a name that leaves the root.
# These four are exactly the errors `Path.is_file()` answers "missing"
# for (`pathlib._IGNORED_ERRNOS` in 3.12) and it raises the rest, so a directory this
# user cannot read still fails the caller instead of quietly dropping a tracked file
# from every lint's file set. The reader is a parameter because that failure is a fault a
# test must cause and a root runner's disk cannot produce: a locked directory still opens
# for it, and gating the test on the uid would spend a skip site this tree may only remove.
_ABSENT = (errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP)


def _regular_paths(
    root: Path, names: list[str], *, lstat: Callable[[str], os.stat_result]
) -> list[str]:
    root_text = str(root).rstrip("/") or "/"
    links: dict[str, bool] = {}
    kept: list[str] = []
    for name in names:
        full = f"{root_text}/{name}"
        try:
            mode = lstat(full).st_mode
        except OSError as error:
            if error.errno not in _ABSENT:
                raise
            continue
        if not stat.S_ISREG(mode):
            continue
        if _link_free(full.rsplit("/", 1)[0], root_text, links):
            kept.append(name)
    return kept


def _link_free(directory: str, root: str, links: dict[str, bool]) -> bool:
    chain: list[str] = []
    answer = links.get(directory)
    while answer is None:
        if directory == root:
            answer = True
            break
        parent = directory.rsplit("/", 1)[0]
        if parent == directory:
            answer = False
            break
        chain.append(directory)
        directory = parent
        answer = links.get(directory)
    for path in reversed(chain):
        answer = answer and not Path(path).is_symlink()
        links[path] = answer
    return answer


# meiva:note
# Fixed Git argv and NUL framing preserve tracked names containing whitespace or newlines.
def tracked_files(
    repo_root: Path,
    *,
    include_untracked: bool = False,
    regular: bool = False,
    lstat: Callable[[str], os.stat_result] = os.lstat,
) -> list[str]:
    extra = ("--cached", "--others", "--exclude-standard") if include_untracked else ()
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo_root), "ls-files", "-z", *extra],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise MeizelError(f"git ls-files failed: {result.stderr.decode(errors='replace').strip()}")
    names = [path.decode() for path in result.stdout.split(b"\0") if path]
    if not regular:
        return names
    return _regular_paths(repo_root, names, lstat=lstat)


def load_universe(root: Path, *, include_untracked: bool = False) -> Universe:
    repo = GitRepo(root)
    files = sorted(set(tracked_files(root, include_untracked=include_untracked, regular=True)))
    root_manifest = repo.load_text(MANIFEST) if MANIFEST in files else ""
    projects = {
        name: repo.load_text(name)
        for name in project_metadata_paths(frozenset(files), root_manifest)
    }
    return Universe(
        repo, files, projects=standalone_projects(frozenset(files), root_manifest, projects)
    )
