# meiva:note
# The checkout this tool reads: git, the text of its files, and the plans applied to it.
#
# The effect shell of `meizel.graph.universe`: the universe holds what a checkout contains, this
# module is the only place that runs git against it, reads a file from it, or writes one back.
# `mode = "none"` in `meizel/meizel.toml` records that split.
# meiva:link meizel/docs/checker/purity.meiva.okf.md

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Callable, Collection, Generator, Mapping, Sequence
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from meizel.graph import project_metadata_paths, python_reverse_closure, standalone_projects
from meizel.graph.model import MeizelError
from meizel.graph.universe import MANIFEST, SOURCE_EXTENSIONS, EditPlan, Universe
from meizel.roles import parse_roles
from meizel.test_graph import (
    build_test_checkout,
    decode_raw_facts,
    encode_raw_facts,
    hash_test_tree,
    is_test_config_file,
    resolve_test_facts,
    scan_raw_test_facts,
)
from meizel.test_graph.impact import select_impacted_tests
from meizel.test_graph.lanes import derive_test_lanes
from meizel.test_graph.model import (
    CutoffContext,
    FileTestFacts,
    ModuleTable,
    RawFileTestFacts,
    TestCheckout,
    TestLanes,
    TestSelection,
)

__all__ = [
    "GitRepo",
    "apply_plan",
    "has_file",
    "lanes_at",
    "load_universe",
    "python_reverse_closure_at",
    "read_bytes",
    "read_roles",
    "read_text",
    "roles_at",
    "select_tests_at",
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
# Reverse Python import closure for a checkout root, returning None on any read or syntax error.
def python_reverse_closure_at(
    root: Path, changed_files: Sequence[str], *, jobs: int | None = None
) -> frozenset[str] | None:
    return python_reverse_closure(load_universe(root), changed_files, jobs=jobs)


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
    _memo: dict[str, object] = field(default_factory=dict[str, object], repr=False, compare=False)

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
        specs = [f"*{s}" for s in suffixes] if suffixes else ["."]
        done = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.root), "grep", "-l", "-F", "-e", marker, "--", *specs],
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

    def untracked_files(self) -> tuple[str, ...]:
        # meiva:note
        # Lists non-ignored untracked files via fixed git ls-files argv without invoking a shell.
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(self.root), "ls-files", "--others", "--exclude-standard", "-z"],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0:
            raise MeizelError(
                f"git ls-files failed: {result.stderr.decode(errors='replace').strip()}"
            )
        candidates = [
            name
            for raw in result.stdout.split(b"\0")
            if raw
            and (
                (name := raw.decode()).endswith(SOURCE_EXTENSIONS)
                or name.rsplit("/", 1)[-1] == MANIFEST
            )
        ]
        if not candidates:
            return ()
        return tuple(sorted(set(_regular_paths(self.root, candidates, lstat=os.lstat))))

    def file_digests(self) -> dict[str, str]:
        if _common_git_dir(self.root) is None:
            return {}
        return _working_tree_digests(self.root)

    def scanner_digest(self) -> str:
        return _process_scanner_digest()

    def cache_get_many(self, keys: Collection[str]) -> dict[str, str]:
        unique = list(dict.fromkeys(keys))
        if not unique:
            return {}
        common = _common_git_dir(self.root)
        if common is None:
            return {}
        db_path = common / _CACHE_DB
        try:
            if not db_path.is_file():
                return {}
            with closing(sqlite3.connect(db_path, timeout=5.0)) as conn:
                rows = conn.execute(_CACHE_GET_SQL, (json.dumps(unique),)).fetchall()
            return {str(key): str(val) for key, val in rows}
        except (sqlite3.Error, OSError):
            return {}

    def cache_put_many(self, entries: Mapping[str, str]) -> None:
        if not entries:
            return
        common = _common_git_dir(self.root)
        if common is None:
            return
        db_path = common / _CACHE_DB
        with (
            suppress(sqlite3.Error, OSError),
            closing(sqlite3.connect(db_path, timeout=5.0)) as conn,
        ):
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(_CACHE_DDL)
            with conn:
                conn.executemany(_CACHE_PUT_SQL, entries.items())

    def memo_get(self, key: str) -> object | None:
        return self._memo.get(key)

    def memo_put(self, key: str, value: object) -> None:
        if len(self._memo) >= 32_768:
            self._memo.clear()
        self._memo[key] = value

    # meiva:note
    # The file index of this checkout: the tracked regular files and the directories above them.
    # Built here, where the repository is read, so a caller holds the value instead of a path it
    # would rebuild the index from.
    def universe(self) -> Universe:
        return load_universe(self.root, repo=self)

    def test_lanes(self) -> TestLanes:
        return lanes_at(self.root, repo=self)

    def select_tests(
        self, changed_paths: Sequence[str], *, base_ref: str | None = None
    ) -> TestSelection:
        return select_tests_at(self.root, changed_paths, base_ref=base_ref, repo=self)

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
                yield GitRepo(target, _memo=self._memo)
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


def load_universe(
    root: Path, *, include_untracked: bool = False, repo: GitRepo | None = None
) -> Universe:
    seat = repo if repo is not None else GitRepo(root)
    files = sorted(set(tracked_files(root, include_untracked=include_untracked, regular=True)))
    root_manifest = seat.load_text(MANIFEST) if MANIFEST in files else ""
    projects = {
        name: seat.load_text(name)
        for name in project_metadata_paths(frozenset(files), root_manifest)
    }
    return Universe(
        seat, files, projects=standalone_projects(frozenset(files), root_manifest, projects)
    )


_CACHE_DB = "meizel-scan-cache-v1.sqlite3"
_CACHE_DDL = (
    "CREATE TABLE IF NOT EXISTS scan_cache (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID"
)
_CACHE_GET_SQL = "SELECT s.k, s.v FROM scan_cache AS s JOIN json_each(?) AS j ON s.k = j.value"
_CACHE_PUT_SQL = "INSERT OR REPLACE INTO scan_cache (k, v) VALUES (?, ?)"
_COMMON_DIRS: dict[tuple[Path, int], Path] = {}


def _common_git_dir(root: Path) -> Path | None:
    try:
        ino = (root / ".git").lstat().st_ino
        resolved = root.resolve()
    except OSError:
        return None
    cached = _COMMON_DIRS.get((resolved, ino))
    if cached is not None:
        return cached
    # meiva:note
    # Resolves the checkout toplevel and shared git-common-dir via fixed git rev-parse argv.
    done = subprocess.run(  # noqa: S603
        [
            "git",
            "-C",
            str(root),
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    lines = done.stdout.splitlines() if done.returncode == 0 else []
    if len(lines) != 2:
        return None
    try:
        if Path(lines[0]).resolve() != resolved:
            return None
        common = Path(lines[1])
        if not common.is_dir():
            return None
    except OSError:
        return None
    _COMMON_DIRS[(resolved, ino)] = common
    return common


def _working_tree_digests(root: Path) -> dict[str, str]:
    # meiva:note
    # Reads git index blob SHAs and dirty working-tree paths via fixed git argv without a shell.
    staged = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "ls-files", "-s", "-z"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if staged.returncode != 0:
        return {}
    # meiva:note
    # Detects working-tree modifications (including racy-git entries) via fixed git diff-files argv.
    modified = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "diff-files", "--name-only", "-z"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if modified.returncode != 0:
        return {}
    digests: dict[str, str] = {}
    dirty: set[str] = {raw.decode() for raw in modified.stdout.split(b"\0") if raw}
    for record in staged.stdout.split(b"\0"):
        if not record:
            continue
        meta, sep, raw_path = record.partition(b"\t")
        if not sep:
            continue
        path = raw_path.decode()
        parts = meta.split()
        if len(parts) == 3 and parts[2] == b"0":
            digests[path] = f"git:{parts[1].decode()}"
        else:
            dirty.add(path)
    for path in dirty:
        try:
            data = (root / path).read_bytes()
        except OSError:
            digests.pop(path, None)
            continue
        digests[path] = f"raw:{hashlib.sha256(data).hexdigest()}"
    return digests


@cache
def _process_scanner_digest() -> str:
    pkg_dir = Path(__file__).resolve().parent
    hasher = hashlib.sha256()
    ver = sys.version_info
    hasher.update(f"py:{ver.major}.{ver.minor}.{ver.micro}\0".encode())
    sources = sorted(
        (path.relative_to(pkg_dir).as_posix(), path)
        for path in pkg_dir.rglob("*")
        if path.suffix in {".py", ".mjs"}
        and "tests" not in path.relative_to(pkg_dir).parts
        and "__pycache__" not in path.relative_to(pkg_dir).parts
        and path.is_file()
    )
    for rel, path in sources:
        hasher.update(rel.encode())
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    policy_path = pkg_dir.parent / "meizel.policy.toml"
    if policy_path.is_file():
        hasher.update(b"policy\0")
        hasher.update(policy_path.read_bytes())
    return hasher.hexdigest()


_SKIP_WALK_DIRS = frozenset(
    {
        ".git",
        ".venv",
        ".worktrees",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "tmp",
        "dist",
        "build",
    }
)
_LANES_MEMO: dict[str, TestLanes] = {}


def _walk_non_git_files(root: Path) -> list[str]:
    found: list[str] = []
    stack: list[tuple[str, Path]] = [("", root)]
    while stack:
        rel_dir, abs_dir = stack.pop()
        with suppress(OSError):
            for entry in os.scandir(abs_dir):
                name = entry.name
                if name in _SKIP_WALK_DIRS or entry.is_symlink():
                    continue
                if name.startswith(".") and name != ".python-version":
                    continue
                rel = f"{rel_dir}/{name}" if rel_dir else name
                if entry.is_dir(follow_symlinks=False):
                    stack.append((rel, Path(entry.path)))
                elif entry.is_file(follow_symlinks=False):
                    found.append(rel)
    return sorted(found)


def _git_test_digests(root: Path) -> dict[str, str]:
    # meiva:note
    # Lists non-ignored untracked test/config files via fixed git ls-files argv without a shell.
    digests = _working_tree_digests(root)
    others = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if others.returncode == 0:
        for raw in others.stdout.split(b"\0"):
            rel = raw.decode(errors="replace") if raw else ""
            if rel and (rel.endswith(".py") or is_test_config_file(rel)):
                with suppress(OSError):
                    digests[rel] = f"raw:{hashlib.sha256((root / rel).read_bytes()).hexdigest()}"
    regular = set(_regular_paths(root, list(digests), lstat=os.lstat))
    return {k: v for k, v in digests.items() if k in regular}


def _resolve_graph_state(
    all_files: list[str], root: Path, raw_map: Mapping[str, RawFileTestFacts]
) -> tuple[TestCheckout, dict[str, FileTestFacts]]:
    configs = {rel: read_text(root, rel) for rel in all_files if is_test_config_file(rel)}
    checkout = build_test_checkout(all_files, configs, raw_map)
    table = ModuleTable.build(checkout.python_files, checkout.top_packages)
    return checkout, resolve_test_facts(checkout, raw_map, table)


def _load_cached_raw_facts(
    root: Path, repo: GitRepo, py_files: Sequence[str], tree_digests: Mapping[str, str]
) -> dict[str, RawFileTestFacts]:
    ver = _process_scanner_digest()
    keys = {rel: f"pytest-facts:v1:{ver}:{rel}:{tree_digests[rel]}" for rel in py_files}
    cached = repo.cache_get_many(keys.values())
    raw_map: dict[str, RawFileTestFacts] = {}
    updates: dict[str, str] = {}
    for rel, ckey in keys.items():
        if (hit := cached.get(ckey)) is not None:
            raw_map[rel] = decode_raw_facts(hit)
        else:
            raw_map[rel] = scanned = scan_raw_test_facts(rel, read_text(root, rel))
            updates[ckey] = encode_raw_facts(scanned)
    repo.cache_put_many(updates)
    return raw_map


def _load_test_graph_state(
    root: Path, repo: GitRepo, digests: dict[str, str] | None = None
) -> tuple[TestCheckout, dict[str, FileTestFacts]]:
    if _common_git_dir(root) is None:
        all_files = _walk_non_git_files(root)
        raw_map = {
            rel: scan_raw_test_facts(rel, read_text(root, rel))
            for rel in all_files
            if rel.endswith(".py") and not rel.startswith("tests/e2e/")
        }
        return _resolve_graph_state(all_files, root, raw_map)
    tree_digests = digests if digests is not None else _git_test_digests(root)
    all_files = sorted(tree_digests)
    py_files = [r for r in all_files if r.endswith(".py") and not r.startswith("tests/e2e/")]
    return _resolve_graph_state(
        all_files, root, _load_cached_raw_facts(root, repo, py_files, tree_digests)
    )


def lanes_at(root: Path, *, repo: GitRepo | None = None) -> TestLanes:
    seat = repo if repo is not None else GitRepo(root)
    if _common_git_dir(root) is None:
        checkout, facts = _load_test_graph_state(root, seat)
        return derive_test_lanes(checkout, facts)
    digests = _git_test_digests(root)
    lanes_key = f"pytest-lanes:v1:{hash_test_tree(_process_scanner_digest(), digests)}"
    if (memo := _LANES_MEMO.get(lanes_key)) is not None:
        return memo
    if (hit := seat.cache_get_many([lanes_key]).get(lanes_key)) is not None:
        decoded = TestLanes.decode(hit)
        _LANES_MEMO[lanes_key] = decoded
        return decoded
    checkout, facts = _load_test_graph_state(root, seat, digests=digests)
    lanes = derive_test_lanes(checkout, facts)
    seat.cache_put_many({lanes_key: lanes.encode()})
    _LANES_MEMO[lanes_key] = lanes
    return lanes


def _load_cutoff_context(
    seat: GitRepo, checkout: TestCheckout, changed_paths: Sequence[str], base_ref: str
) -> CutoffContext | None:
    # meiva:note
    # Lists tracked files at `base_ref` via fixed git ls-tree argv without invoking a shell.
    listing = subprocess.run(  # noqa: S603
        ["git", "-C", str(seat.root), "ls-tree", "-r", "--name-only", "-z", base_ref],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if listing.returncode != 0:
        return None
    base_files = [raw.decode(errors="replace") for raw in listing.stdout.split(b"\0") if raw]
    base_configs = {
        rel: txt
        for rel in base_files
        if is_test_config_file(rel) and (txt := seat.show_optional_text(base_ref, rel)) is not None
    }
    base_checkout = build_test_checkout(base_files, base_configs, {})
    base_sources = {
        rel: txt
        for rel in changed_paths
        if rel.endswith(".py") and (txt := seat.show_optional_text(base_ref, rel)) is not None
    }
    head_sources = {
        rel: read_text(seat.root, rel)
        for rel in changed_paths
        if rel.endswith(".py") and has_file(seat.root, rel)
    }
    return CutoffContext(
        base_sources=base_sources,
        head_sources=head_sources,
        base_manifests=base_checkout.manifests,
        head_manifests=checkout.manifests,
        base_test_dirs=base_checkout.test_dirs,
        base_collectable=base_checkout.collectable,
    )


def select_tests_at(
    root: Path,
    changed_paths: Sequence[str],
    *,
    base_ref: str | None = None,
    repo: GitRepo | None = None,
) -> TestSelection:
    seat = repo if repo is not None else GitRepo(root)
    checkout, facts = _load_test_graph_state(root, seat)
    cutoff = (
        _load_cutoff_context(seat, checkout, changed_paths, base_ref)
        if base_ref is not None
        else None
    )
    return select_impacted_tests(tuple(changed_paths), checkout, facts, cutoff=cutoff)
