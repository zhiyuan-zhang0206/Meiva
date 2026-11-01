from __future__ import annotations

import subprocess
from collections.abc import Collection, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from meizel.allowances import Policy
from meizel.checkout import GitRepo
from meizel.graph.model import (
    Acquisition,
    AmbientKind,
    AnyCollaboratorSite,
    Code,
    EntropySite,
    EnumSubsetUse,
    Fact,
    FactKind,
    Patch,
    Problem,
    PurityItem,
    Restricted,
    Sources,
    TypeCastSite,
    Unless,
)
from meizel.graph.universe import Checkout, EditPlan, Universe
from meizel.language.fixtures import FixtureFunction, FixtureRequest, FixtureState
from meizel.language.input_mutations import InputListAppend
from meizel.language.scan import FileScan
from meizel.language.scan_cache import (
    cached_module_imports,
    decode_file_scan,
    encode_file_scan,
    load_cached_sources,
    scan_all,
    store_cached_sources,
)


@dataclass
class _RecordingCheckout:
    inner: GitRepo
    loaded_bytes: list[str] = field(default_factory=list[str])

    @property
    def root(self) -> Path:
        return self.inner.root

    def has_file(self, rel: str) -> bool:
        return self.inner.has_file(rel)

    def load_text(self, rel: str, *, default: str | None = None) -> str:
        return self.inner.load_text(rel, default=default)

    def load_bytes(self, rel: str) -> bytes:
        self.loaded_bytes.append(rel)
        return self.inner.load_bytes(rel)

    def run_text(self, *args: str) -> str:
        return self.inner.run_text(*args)

    def run_bytes(self, *args: str, stdin: bytes | None = None) -> bytes:
        return self.inner.run_bytes(*args, stdin=stdin)

    def default_base(self) -> str:
        return self.inner.default_base()

    def show_optional_text(self, ref: str, path: str) -> str | None:
        return self.inner.show_optional_text(ref, path)

    def show_bytes(self, ref: str, path: str) -> bytes:
        return self.inner.show_bytes(ref, path)

    def stage_all(self) -> None:
        self.inner.stage_all()

    def apply_plan(self, plan: EditPlan) -> None:
        self.inner.apply_plan(plan)

    def grep_files(self, marker: str, suffixes: tuple[str, ...]) -> list[str]:
        return self.inner.grep_files(marker, suffixes)

    def grep_words(self, words: Collection[str], suffix: str) -> list[str]:
        return self.inner.grep_words(words, suffix)

    def untracked_files(self) -> tuple[str, ...]:
        return self.inner.untracked_files()

    def file_digests(self) -> dict[str, str]:
        return self.inner.file_digests()

    def scanner_digest(self) -> str:
        return self.inner.scanner_digest()

    def cache_get_many(self, keys: Collection[str]) -> dict[str, str]:
        return self.inner.cache_get_many(keys)

    def cache_put_many(self, entries: Mapping[str, str]) -> None:
        self.inner.cache_put_many(entries)

    def memo_get(self, key: str) -> object | None:
        return self.inner.memo_get(key)

    def memo_put(self, key: str, value: object) -> None:
        self.inner.memo_put(key, value)

    def universe(self) -> Universe:
        base = self.inner.universe()
        return Universe(self, sorted(base.files), projects=base.standalone_projects)

    @contextmanager
    def checkout(self, ref: str) -> Generator[Checkout]:
        with self.inner.checkout(ref) as child:
            yield _RecordingCheckout(child)


def _init_git_repo(base: Path, files: Mapping[str, str]) -> Path:
    root = base / "repo"
    root.mkdir()
    # meiva:note
    # Fixed git init argv on a temporary directory without invoking a shell.
    subprocess.run(["git", "init", "--initial-branch=main", "-q", str(root)], check=True)  # noqa: S603
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    # meiva:note
    # Fixed git add argv on the temporary checkout without invoking a shell.
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)  # noqa: S603
    return root


def test_encode_decode_file_scan_round_trips_full_and_sparse_forms() -> None:
    minimal = FileScan("pkg/empty.py")
    encoded_minimal = encode_file_scan(minimal)
    assert encoded_minimal == '{"path":"pkg/empty.py"}'
    assert decode_file_scan(encoded_minimal) == minimal

    populated = FileScan(
        "pkg/full.py",
        facts=[
            Fact(
                "pkg/full.py",
                "pkg/dep.py",
                3,
                FactKind.IMPORT,
                ("run", "stop"),
                "from pkg.dep import run, stop",
            )
        ],
        restricted=[Restricted(7, "exec", "exec of source text")],
        patches=[Patch(9, "mock.patch", "pkg.dep.run", Code.FIRST_PARTY_PATCH, imported=True)],
        problems=[Problem(11, Code.UNRESOLVED_IMPORT, "unresolved import pkg.missing")],
        fixtures=FixtureState(
            defs={"db_conn"},
            requests=[
                FixtureRequest("db_conn", 14, skip_self=False, requester_line=13, form="parameter"),
                FixtureRequest(
                    "clean_tmp", 15, skip_self=True, requester_line=15, form="usefixtures"
                ),
                FixtureRequest("seed", 16, skip_self=False, requester_line=16, form="call"),
                FixtureRequest("dyn", 17, skip_self=False, requester_line=17, form="lookup"),
            ],
            plugins=[("pkg.plugins", 2)],
            functions=[FixtureFunction("db_conn", 13, "db_conn", True, True)],
        ),
        imported={"pkg/dep.py"},
        purity=[PurityItem(20, Code.IMPURE_CALL, "fn#open", "calls open", "inject reader")],
        enum_subsets=[EnumSubsetUse(22, "Color", ("RED", "BLUE"))],
        input_appends=[InputListAppend(25, "collect", "items")],
        ambient=[Acquisition(28, AmbientKind.CLOCK, "time.time")],
        entropy=[EntropySite(30, "uuid.uuid4")],
        type_casts=[TypeCastSite(32, 11, "fn")],
        any_collaborators=[AnyCollaboratorSite(35, 8, "Box.run", "arg", "parameter")],
        type_checking=(4, 5),
        symbols=(("Box", 34, 40), ("fn", 19, 33)),
        deps=("pkg/dep.py",),
    )
    assert decode_file_scan(encode_file_scan(populated)) == populated


def test_scan_all_hits_sqlite_cache_and_invalidates_on_same_length_edit(tmp_path: Path) -> None:
    root = _init_git_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/b.py": "VALUE = 1\n",
            "pkg/a.py": "from pkg.b import VALUE\n",
        },
    )
    policy = Policy()
    sources = Sources()
    first_repo = _RecordingCheckout(GitRepo(root))
    first = scan_all(first_repo.universe(), sources, jobs=1, policy=policy)
    assert sorted(first_repo.loaded_bytes) == ["pkg/__init__.py", "pkg/a.py", "pkg/b.py"]
    assert (root / ".git" / "meizel-scan-cache-v1.sqlite3").is_file()

    second_repo = _RecordingCheckout(GitRepo(root))
    second = scan_all(second_repo.universe(), sources, jobs=1, policy=policy)
    assert second_repo.loaded_bytes == []
    assert second == first

    # meiva:note
    # Same-length unstaged edit exercises git diff-files working-tree digest invalidation.
    (root / "pkg/b.py").write_text("OTHER = 2\n", encoding="utf-8")
    (root / "pkg/a.py").write_text("from pkg.b import OTHER\n", encoding="utf-8")
    third_repo = _RecordingCheckout(GitRepo(root))
    third = scan_all(third_repo.universe(), sources, jobs=1, policy=policy)
    assert sorted(third_repo.loaded_bytes) == ["pkg/a.py", "pkg/b.py"]
    assert third["pkg/a.py"].facts[0].names == ("OTHER",)


def test_cross_file_patch_target_deps_invalidate_when_target_module_changes(
    tmp_path: Path,
) -> None:
    root = _init_git_repo(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/target.py": "def run() -> int:\n    return 1\n",
            "pkg/tests/test_target.py": (
                "from unittest import mock\n"
                "import pkg.target\n"
                "def test_it() -> None:\n"
                '    mock.patch.object(pkg.target, "run")\n'
            ),
        },
    )
    policy = Policy()
    sources = Sources()
    initial = scan_all(GitRepo(root).universe(), sources, jobs=1, policy=policy)
    assert initial["pkg/tests/test_target.py"].deps == ("pkg/target.py",)
    assert [p.code for p in initial["pkg/tests/test_target.py"].patches] == [Code.FIRST_PARTY_PATCH]
    assert initial["pkg/tests/test_target.py"].problems == []

    warm_repo = _RecordingCheckout(GitRepo(root))
    warm = scan_all(warm_repo.universe(), sources, jobs=1, policy=policy)
    assert warm_repo.loaded_bytes == []
    assert warm["pkg/tests/test_target.py"].problems == []

    # meiva:note
    # Modify only pkg/target.py; pkg/tests/test_target.py is untouched on disk and in the index.
    (root / "pkg/target.py").write_text("def renamed() -> int:\n    return 2\n", encoding="utf-8")
    after_repo = _RecordingCheckout(GitRepo(root))
    after = scan_all(after_repo.universe(), sources, jobs=1, policy=policy)
    assert "pkg/tests/test_target.py" in after_repo.loaded_bytes
    assert [p.code for p in after["pkg/tests/test_target.py"].problems] == [
        Code.MISSING_PATCH_TARGET
    ]


def test_sources_and_corrupt_sqlite_entries_degrade_gracefully(tmp_path: Path) -> None:
    root = _init_git_repo(tmp_path, {"pkg/__init__.py": "", "pkg/a.py": "VALUE = 1\n"})
    policy = Policy()
    repo = GitRepo(root)
    universe = repo.universe()

    sources = Sources(
        names={"os.environ": AmbientKind.ENVIRON},
        calls={"time.time": AmbientKind.CLOCK},
        uses=frozenset({"pkg/a.py"}),
        unless={"fn": Unless("clock", 0), "kw": Unless("now", None)},
    )
    key, miss = load_cached_sources(universe, "state-1", policy)
    assert key.startswith("sources:v1:")
    assert miss is None
    store_cached_sources(universe, key, sources)
    _, hit = load_cached_sources(universe, "state-1", policy)
    assert hit == sources
    _, sqlite_hit = load_cached_sources(GitRepo(root).universe(), "state-1", policy)
    assert sqlite_hit == sources

    calls: list[str] = []

    def _compute(_u: Universe, path: str) -> dict[str, str]:
        calls.append(path)
        return {"x": "pkg.x"}

    assert cached_module_imports(universe, ["pkg/a.py"], _compute) == {"pkg/a.py": {"x": "pkg.x"}}
    assert calls == ["pkg/a.py"]
    assert cached_module_imports(universe, ["pkg/a.py"], _compute) == {"pkg/a.py": {"x": "pkg.x"}}
    assert calls == ["pkg/a.py"]

    repo.cache_put_many({key: "{not-json"})
    _, corrupted_sources = load_cached_sources(GitRepo(root).universe(), "state-1", policy)
    assert corrupted_sources is None

    db_path = root / ".git" / "meizel-scan-cache-v1.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        candidate = root / ".git" / f"meizel-scan-cache-v1.sqlite3{suffix}"
        if candidate.exists():
            candidate.unlink()
    db_path.write_bytes(b"corrupt sqlite header bytes")
    recovered = scan_all(GitRepo(root).universe(), Sources(), jobs=1, policy=policy)
    assert set(recovered) == {"pkg/__init__.py", "pkg/a.py"}
