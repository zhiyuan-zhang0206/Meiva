from __future__ import annotations

import pytest

from meizel.contracts.analysis import render_policy
from meizel.contracts.cli import GitRepo
from meizel.language.scan import is_imported_binding, module_bindings
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, git, run


def test_manifest_purity_accepts_top_level_and_table_forms(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/pure.py": "def add(x: int, y: int) -> int:\n    return x + y\n",
            "pkg/impure.py": "import subprocess\n",
            "pkg/meizel.toml": '[purity]\n"pure.py" = "strict"\n',
        }
    )
    assert analysis_of(root).verdict == []


def test_manifest_purity_rejects_unknown_levels_and_unknown_children(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "",
            "pkg/meizel.toml": '[purity]\n"a.py" = "bogus"\n"missing.py" = "strict"\n',
        }
    )
    assert len(diagnostics(root, "manifest-syntax")) == 1
    assert len(diagnostics(root, "manifest-unknown-child")) == 1


def test_impure_edge_fails_in_strict_and_records_in_strict_to_be(
    make_repo: RepoFactory,
) -> None:
    files = {
        "core/__init__.py": "",
        "core/domain.py": "from infra import db\n",
        "core/meizel.toml": 'purity = "strict"\n',
        "infra/__init__.py": "",
        "infra/db.py": "X = 1\n",
        "infra/meizel.toml": 'exports = ["db.py"]\n',
        "meizel.toml": '[uses]\n"core" = ["infra"]\n',
    }
    root = make_repo(files)
    [found] = diagnostics(root, "impure-edge")
    assert found.debt is None
    assert "core/domain.py -> infra/db.py" in found.message
    assert run(root, "debt", "record", "core") == 1

    (root / "core/meizel.toml").write_text('purity = "strict-to-be"\n', encoding="utf-8")
    git(root, "add", "-A")
    assert run(root, "debt", "record", "core") == 0
    git(root, "add", "-A")
    assert '[debt.impure-edge]\n"domain.py#infra/db.py" = 1\n' in (
        root / "core/meizel.toml"
    ).read_text(encoding="utf-8")
    assert analysis_of(root).verdict == []


@pytest.mark.parametrize(
    ("source", "expected_key"),
    [
        ("import subprocess\n", "a.py#import subprocess"),
        ("from httpx import Client\n", "a.py#import httpx"),
        ("async def fetch() -> int:\n    return 1\n", "a.py#async def fetch"),
        ("import time\n\ndef now() -> float:\n    return time.time()\n", "a.py#time.time"),
        (
            "from datetime import datetime\n\ndef now() -> object:\n    return datetime.now()\n",
            "a.py#datetime.now",
        ),
        ("import os\n\ndef env() -> str:\n    return os.environ.get('A', '')\n", "a.py#os.environ"),
        ("import os\n\ndef env() -> str | None:\n    return os.getenv('A')\n", "a.py#os.getenv"),
        ("def read() -> str:\n    return open('x').read()\n", "a.py#open"),
        ("import uuid\n\ndef uid() -> object:\n    return uuid.uuid4()\n", "a.py#uuid.uuid4"),
        (
            "import random\n\ndef pick() -> int:\n    return random.randint(1, 2)\n",
            "a.py#random.randint",
        ),
        (
            "from pathlib import Path\n\ndef load(p: Path) -> str:\n    return p.read_text()\n",
            "a.py#Path.read_text",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n    p.replace(q)\n",
            "a.py#Path.replace",
        ),
        ("import os\n\ndef size(p: str) -> int:\n    return os.stat(p).st_size\n", "a.py#os.stat"),
        ("import os\n\ndef lstat(p: str) -> object:\n    return os.lstat(p)\n", "a.py#os.lstat"),
        ("import os\n\ndef grab(p: str) -> int:\n    return os.open(p, 0)\n", "a.py#os.open"),
        ("import os\n\ndef pull(fd: int) -> bytes:\n    return os.read(fd, 8)\n", "a.py#os.read"),
        ("import os\n\ndef shape(fd: int) -> object:\n    return os.fstat(fd)\n", "a.py#os.fstat"),
        (
            "import os\n\ndef names(p: str) -> list[str]:\n    return os.listdir(p)\n",
            "a.py#os.listdir",
        ),
        ("import os\n\ndef scan(p: str) -> object:\n    return os.scandir(p)\n", "a.py#os.scandir"),
        ("import os\n\ndef tree(p: str) -> object:\n    return os.walk(p)\n", "a.py#os.walk"),
        (
            "import os\n\ndef here(p: str) -> bool:\n    return os.path.isdir(p)\n",
            "a.py#os.path.isdir",
        ),
        (
            "import pkgutil\n\ndef blob() -> bytes | None:\n    return pkgutil.get_data('a', 'b')\n",
            "a.py#pkgutil.get_data",
        ),
        (
            "import inspect\n\ndef source(f: object) -> str:\n    return inspect.getsource(f)\n",
            "a.py#inspect.getsource",
        ),
        (
            "from pathlib import Path\n\ndef kids(p: Path) -> object:\n    return Path('x').iterdir()\n",
            "a.py#Path.iterdir",
        ),
        (
            "from pathlib import Path\n\ndef all_py() -> object:\n    return Path('x').rglob('*.py')\n",
            "a.py#Path.rglob",
        ),
        (
            "from pathlib import Path\n\ndef link() -> bool:\n    return Path('x').is_symlink()\n",
            "a.py#Path.is_symlink",
        ),
        (
            "from pathlib import Path\n\ndef where() -> Path:\n    return Path('x').resolve()\n",
            "a.py#Path.resolve",
        ),
        (
            "from pathlib import Path\n\ndef hits() -> object:\n    return Path('x').glob('*')\n",
            "a.py#Path.glob",
        ),
        ("from pathlib import Path\n\n_is_file = Path.is_file\n", "a.py#Path.is_file"),
        ("import os\n\nread_info = os.stat\n", "a.py#os.stat"),
        ("import time\n\nnap = time.sleep\n", "a.py#time.sleep"),
        (
            "def settle(future: object) -> object:\n    return future.resolve()\n",
            "a.py#Path.resolve",
        ),
        (
            "def hits(spec: object) -> object:\n    return spec.root().glob('*')\n",
            "a.py#Path.glob",
        ),
        ("def look(handle: object) -> object:\n    return handle.open()\n", "a.py#Path.open"),
        (
            "from pathlib import Path\n\ndef each(root: Path) -> None:\n"
            "    for entry in root.iterdir():\n        entry.replace(root)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef each(root: Path) -> None:\n"
            "    for entry in sorted(root.rglob('*.py')):\n        entry.replace(root)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef each(root: Path) -> list[Path]:\n"
            "    return [p.replace(root) for p in root.rglob('*.py')]\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\nclass Box:\n"
            "    def __init__(self, root: Path) -> None:\n        self.root = root\n\n"
            "    def look(self, target: Path) -> None:\n        self.root.replace(target)\n",
            "a.py#Path.replace",
        ),
        (
            "from dataclasses import dataclass\nfrom pathlib import Path\n\n"
            "@dataclass(frozen=True)\nclass Box:\n    root: Path\n\n"
            "    def look(self, target: Path) -> Path:\n        return self.root.replace(target)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef each(paths: list[Path], to: Path) -> None:\n"
            "    for path in dict.fromkeys(paths):\n        path.replace(to)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.joinpath('x').replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    (p / 'x').replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.relative_to(q).replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.absolute().replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.parent.replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.with_suffix('.bak').replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.with_name('x').replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.with_stem('x').replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.expanduser().replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.resolve().replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(p: Path, q: Path) -> None:\n"
            "    p.parents[1].replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef move(q: Path) -> None:\n    Path.home().replace(q)\n",
            "a.py#Path.replace",
        ),
        (
            "from pathlib import Path\n\ndef later(root: Path) -> list[object]:\n"
            "    return [entry.stat for entry in root.iterdir()]\n",
            "a.py#Path.stat",
        ),
        (
            "from pathlib import Path\n\ndef keep(xs: list[Path]) -> list[bool]:\n"
            "    return list(map(Path.is_file, xs))\n",
            "a.py#Path.is_file",
        ),
        (
            "import os\nfrom functools import partial\n\nstat_of = partial(os.stat, '.')\n",
            "a.py#os.stat",
        ),
        ("import os\n\ndef f(read: object = os.stat) -> None:\n    return None\n", "a.py#os.stat"),
        ("_open = open\n", "a.py#open"),
        (
            "from pathlib import Path\n\ndef f(p: Path) -> object:\n    return p.stat\n",
            "a.py#Path.stat",
        ),
        ("import os\n\ndef f(a: str, b: str) -> None:\n    os.replace(a, b)\n", "a.py#os.replace"),
        ("import os\n\ndef f(a: str) -> None:\n    os.remove(a)\n", "a.py#os.remove"),
        ("import os\n\ndef f(a: str) -> None:\n    os.makedirs(a)\n", "a.py#os.makedirs"),
        (
            "import os\n\ndef f(a: str) -> str:\n    return os.path.abspath(a)\n",
            "a.py#os.path.abspath",
        ),
        ("import os\n\ndef f(a: str) -> bool:\n    return os.access(a, 0)\n", "a.py#os.access"),
        ("import glob\n\ndef f(a: str) -> list[str]:\n    return glob.glob(a)\n", "a.py#glob.glob"),
        ("import os\n\ndef f(fd: int) -> None:\n    os.fsync(fd)\n", "a.py#os.fsync"),
        (
            "import os\n\ndef f(a: str) -> bool:\n    return os.path.lexists(a)\n",
            "a.py#os.path.lexists",
        ),
        (
            "from pathlib import Path\n\ndef f(p: Path, q: Path) -> None:\n    p.symlink_to(q)\n",
            "a.py#Path.symlink_to",
        ),
    ],
)
def test_impure_call_catches_effectful_imports_async_and_calls(
    make_repo: RepoFactory, source: str, expected_key: str
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": source,
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    found = diagnostics(root, "impure-call")
    assert any(d.debt is not None and d.debt.key == expected_key for d in found)


def test_string_replace_is_allowed_in_strict_module(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": "def clean(s: str) -> str:\n    return s.replace('a', 'b')\n",
            "pkg/meizel.toml": 'purity = "strict"\n',
        }
    )
    assert analysis_of(root).verdict == []


@pytest.mark.parametrize(
    "source",
    [
        "def f(rows: list[str]) -> None:\n    for s in rows:\n        s.replace('a', 'b')\n",
        "def f() -> None:\n    for s in ['a', 'b']:\n        s.replace('a', 'b')\n",
        "def f(rows: list[str]) -> list[str]:\n    return [s.replace('a', 'b') for s in rows]\n",
        "class Box:\n    def __init__(self, name: str) -> None:\n        self.name = name\n\n"
        "    def clean(self) -> str:\n        return self.name.replace('a', 'b')\n",
        "from pathlib import Path\n\ndef f(p: Path) -> str:\n    return p.name.replace('a', 'b')\n",
        "from pathlib import Path\n\ndef f(p: Path) -> str:\n    return str(p).replace('a', 'b')\n",
        "from pathlib import Path\n\ndef f(p: Path) -> list[str]:\n"
        "    return [s.replace('a', 'b') for s in str(p).split('/')]\n",
    ],
)
def test_replace_on_something_that_is_not_a_path_is_not_a_filesystem_call(
    make_repo: RepoFactory, source: str
) -> None:
    root = make_repo(
        {"pkg/__init__.py": "", "pkg/a.py": source, "pkg/meizel.toml": 'purity = "strict-to-be"\n'}
    )
    assert diagnostics(root, "impure-call") == []


def test_unfrozen_value_requires_frozen_dataclass(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "from dataclasses import dataclass\n\n"
                "@dataclass\nclass Bad:\n    x: int\n\n"
                "@dataclass(frozen=False)\nclass AlsoBad:\n    x: int\n\n"
                "@dataclass(frozen=True)\nclass Good:\n    x: int\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    found = diagnostics(root, "unfrozen-value")
    keys = sorted(d.debt.key for d in found if d.debt is not None)
    assert keys == ["a.py#class AlsoBad", "a.py#class Bad"]
    assert all("does not freeze nested values" in d.render() for d in found)


def test_untyped_export_rejects_any_and_bare_dict_on_public_functions(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "from typing import Any\n\n"
                "def bad_any(x: Any) -> int:\n    return 1\n\n"
                "def bad_dict(x: dict) -> dict[str, Any]:\n    return {}\n\n"
                "def _private_ok(x: Any) -> dict:\n    return {}\n\n"
                "def good_typed(x: dict[str, int]) -> list[str]:\n    return list(x)\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    keys = [d.debt.key for d in diagnostics(root, "untyped-export") if d.debt is not None]
    assert keys == ["a.py#def bad_any", "a.py#def bad_dict", "a.py#def bad_dict"]


def test_impure_test_fixture_flags_heavy_fixtures_and_monkeypatch_for_strict_tests(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/domain.py": "def calc(x: int) -> int:\n    return x + 1\n",
            "pkg/meizel.toml": 'purity = "strict"\nexports = ["domain.py"]\n\n[uses]\n"tests" = ["domain.py"]\n',
            "pkg/tests/test_domain.py": (
                "from pkg.domain import calc\n\n"
                "def test_calc(db_conn, monkeypatch):\n"
                "    assert calc(1) == 2\n"
            ),
        }
    )
    found = diagnostics(root, "impure-test-fixture")
    assert len(found) == 2
    assert all(d.debt is None for d in found)


def test_strict_to_be_with_zero_violations_reports_debt_paid(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/domain.py": "def calc(x: int) -> int:\n    return x + 1\n",
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    paid = diagnostics(root, "debt-paid")
    assert len(paid) == 1
    assert "strict-to-be" in paid[0].message


def test_bindings_distinguish_defined_vs_imported_and_flag_imported_patch(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "lib/__init__.py": "",
            "lib/dep.py": "def run() -> int:\n    return 1\n\ndef reexported() -> int:\n    return 2\n",
            "lib/consumer.py": (
                "from lib.dep import reexported, run\n\n"
                "__all__ = ['reexported']\n\n"
                "def local() -> int:\n    return run()\n"
            ),
            "lib/meizel.toml": 'exports = ["consumer.py", "dep.py"]\n\n[uses]\n"consumer.py" = ["dep.py"]\n',
            "app/__init__.py": "",
            "app/test_x.py": (
                "from lib import consumer\n\n"
                "def test_a(monkeypatch):\n"
                "    monkeypatch.setattr(consumer, 'run', lambda: 0)\n"
                "    monkeypatch.setattr(consumer, 'reexported', lambda: 0)\n"
                "    monkeypatch.setattr(consumer, 'local', lambda: 0)\n"
            ),
            "meizel.toml": '[uses]\n"app" = ["lib"]\n',
        }
    )
    b = module_bindings(GitRepo(root).universe(), "lib/consumer.py")
    assert "local" in b.defined
    assert {"run", "reexported"} <= b.imported
    assert "reexported" in b.all_names
    assert is_imported_binding(b, "run") is True
    assert is_imported_binding(b, "reexported") is False
    assert is_imported_binding(b, "local") is False

    patches = {d.debt.key: d for d in diagnostics(root, "first-party-patch") if d.debt is not None}
    assert "(imported binding)" in patches["test_x.py#monkeypatch.setattr lib.consumer.run"].message
    assert (
        "(imported binding)"
        not in patches["test_x.py#monkeypatch.setattr lib.consumer.reexported"].message
    )
    assert (
        "(imported binding)"
        not in patches["test_x.py#monkeypatch.setattr lib.consumer.local"].message
    )


def test_bindings_treat_a_file_outside_the_universe_as_dynamic(
    make_repo: RepoFactory,
) -> None:
    # meiva:note
    # The universe is the snapshot the run admitted: a name it does not hold cannot be read, so
    # the caller's question about what that module binds stays open instead of guessed.
    root = make_repo({"lib/__init__.py": "", "lib/known.py": "def run() -> int:\n    return 1\n"})
    universe = GitRepo(root).universe()
    assert module_bindings(universe, "lib/known.py").dynamic is False
    assert module_bindings(universe, "lib/never_committed.py").dynamic is True


def test_stale_export_flags_unused_subpackage_door_and_prune_removes_it(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/caller.py": "from pkg.sub import used\n",
            "pkg/meizel.toml": '[uses]\n"caller.py" = ["sub"]\n',
            "pkg/sub/__init__.py": "",
            "pkg/sub/used.py": "X = 1\n",
            "pkg/sub/unused.py": "Y = 2\n",
            "pkg/sub/meizel.toml": 'purity = "strict"\nexports = ["unused.py", "used.py"]\n',
        }
    )
    [found] = diagnostics(root, "stale-export")
    assert found.path == "pkg/sub/meizel.toml"
    assert "'unused.py'" in found.message
    assert run(root, "sync", "--prune") == 0
    git(root, "add", "-A")
    assert analysis_of(root).verdict == []
    assert '"unused.py"' not in (root / "pkg/sub/meizel.toml").read_text(encoding="utf-8")


def test_pass_through_function_flags_trivial_wrappers_and_allows_real_adapters(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "def target(x: int, y: int) -> int:\n"
                "    return x + y\n\n"
                "def bad_wrapper(x: int, y: int) -> int:\n"
                "    return target(x, y)\n\n"
                "def good_default(x: int, y: int = 1) -> int:\n"
                "    return target(x, y)\n\n"
                "def good_transform(x: int, y: int) -> int:\n"
                "    return target(x + 1, y)\n\n"
                "def _private_wrapper(x: int, y: int) -> int:\n"
                "    return target(x, y)\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    keys = [d.debt.key for d in diagnostics(root, "pass-through-function") if d.debt is not None]
    assert keys == ["a.py#bad_wrapper"]


def test_nested_attr_write_flags_deep_attribute_mutation(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "def mutate( holder: object, val: int) -> None:\n"
                "    holder.inner.count = val\n"
                "    holder.inner.count += 1\n"
                "    holder.direct = val\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    keys = [d.debt.key for d in diagnostics(root, "nested-attr-write") if d.debt is not None]
    assert keys == ["a.py#holder.inner.count", "a.py#holder.inner.count"]


def test_nested_patch_target_flags_deep_patch_chains_in_strict_tests(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/domain.py": "class Svc:\n    pass\n",
            "pkg/meizel.toml": 'purity = "strict-to-be"\n\n[uses]\n"tests" = ["domain.py"]\n',
            "pkg/tests/test_domain.py": (
                "from pkg import domain\n\n"
                "def test_svc(holder: object) -> None:\n"
                "    _ = domain.Svc\n"
                "    monkeypatch_like = holder\n"
            ),
        }
    )
    (root / "pkg/tests/test_domain.py").write_text(
        "from pkg import domain\n\n"
        "def test_svc(monkeypatch, holder: object) -> None:\n"
        "    _ = domain.Svc\n"
        "    monkeypatch.setattr(holder.inner, 'x', 1)\n",
        encoding="utf-8",
    )
    found = diagnostics(root, "nested-patch-target")
    assert len(found) == 1
    assert found[0].debt is not None
    assert (found[0].debt.owner, found[0].debt.key) == (
        "pkg/tests",
        "test_domain.py#holder.inner.x",
    )


def test_domain_subset_leak_flags_duplicate_cross_module_enum_subsets(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/model.py": (
                "from enum import StrEnum\n\n"
                "class Status(StrEnum):\n"
                "    PENDING = 'pending'\n"
                "    DONE = 'done'\n"
                "    FAILED = 'failed'\n\n"
                "def in_owner(s: Status) -> bool:\n"
                "    return s in (Status.DONE, Status.FAILED)\n"
            ),
            "pkg/first.py": (
                "from pkg.model import Status\n\n"
                "def check_a(s: Status) -> bool:\n"
                "    return s in (Status.DONE, Status.FAILED)\n"
            ),
            "pkg/second.py": (
                "from pkg.model import Status\n\n"
                "def check_b(s: Status) -> bool:\n"
                "    return s not in (Status.FAILED, Status.DONE)\n"
            ),
            "pkg/meizel.toml": (
                'purity = "strict-to-be"\n\n'
                '[uses]\n"first.py" = ["model.py"]\n"second.py" = ["model.py"]\n'
            ),
        }
    )
    keys = sorted(d.debt.key for d in diagnostics(root, "domain-subset-leak") if d.debt is not None)
    assert keys == ["first.py#Status(DONE,FAILED)", "second.py#Status(DONE,FAILED)"]


def test_wide_fanout_flags_excess_sibling_uses_in_strict_and_records_in_strict_to_be(
    make_repo: RepoFactory,
) -> None:
    files = {
        "pkg/__init__.py": "",
        "pkg/hub.py": "from pkg import m1, m2, m3\n",
        "pkg/m1.py": "X = 1\n",
        "pkg/m2.py": "X = 2\n",
        "pkg/m3.py": "X = 3\n",
        "pkg/meizel.toml": 'purity = "strict-to-be"\n\n[uses]\n"hub.py" = ["m1.py", "m2.py", "m3.py"]\n',
        "meizel.policy.toml": '[purity]\nmax-uses = ["2"]\n',
    }
    root = make_repo(files)
    [found] = diagnostics(root, "wide-fanout")
    assert found.debt is not None
    assert found.debt.key == "hub.py#uses"
    assert "depends on 3 sibling targets (max 2" in found.message


def test_wide_exports_flags_excess_subpackage_doors(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/sub/__init__.py": "",
            "pkg/sub/a.py": "X = 1\n",
            "pkg/sub/b.py": "X = 2\n",
            "pkg/sub/c.py": "X = 3\n",
            "pkg/sub/meizel.toml": 'purity = "strict-to-be"\nexports = ["a.py", "b.py", "c.py"]\n',
            "meizel.policy.toml": '[purity]\nmax-exports = ["2"]\n',
        }
    )
    [found] = diagnostics(root, "wide-exports")
    assert found.debt is not None
    assert (found.debt.owner, found.debt.key) == ("pkg/sub", "meizel.toml#exports")


def test_wide_public_surface_counts_behavioral_symbols_not_frozen_data_classes(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "from dataclasses import dataclass\n"
                "from enum import StrEnum\n\n"
                "@dataclass(frozen=True)\nclass Value:\n    x: int\n\n"
                "class Mode(StrEnum):\n    A = 'a'\n\n"
                "class DomainError(Exception):\n    pass\n\n"
                "def f1(v: Value) -> int:\n    return v.x + 1\n\n"
                "def f2(v: Value) -> int:\n    return v.x + 2\n\n"
                "def f3(v: Value) -> int:\n    return v.x + 3\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
            "meizel.policy.toml": '[purity]\nmax-public-symbols = ["2"]\n',
        }
    )
    [found] = diagnostics(root, "wide-public-surface")
    assert found.debt is not None
    assert found.debt.key == "a.py#public-surface"
    assert "3 public behavioral symbols (max 2" in found.message


def test_wide_params_excludes_self_and_flags_excess_parameters(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "from dataclasses import dataclass\n\n"
                "@dataclass(frozen=True)\nclass Svc:\n"
                "    def ok(self, a: int, b: int, c: int) -> int:\n"
                "        return a + b + c\n\n"
                "    def too_many(self, a: int, b: int, c: int, d: int) -> int:\n"
                "        return a + b + c + d\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
            "meizel.policy.toml": '[purity]\nmax-params = ["3"]\n',
        }
    )
    [found] = diagnostics(root, "wide-params")
    assert found.debt is not None
    assert found.debt.key == "a.py#def too_many"


def test_deep_attr_chain_flags_train_wrecks_and_ignores_module_or_method_chains(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "def read(ctx: object, text: str) -> str:\n"
                "    _ = text.strip().lower().replace('a', 'b')\n"
                "    _ = ctx.a.b.c\n"
                "    return str(ctx.a.b.c.d)\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    [found] = diagnostics(root, "deep-attr-chain")
    assert found.debt is not None
    assert found.debt.key == "a.py#ctx.a.b.c.d"


@pytest.mark.parametrize(
    "source",
    [
        "def pick(pattern: object) -> object:\n    return pattern.glob\n",
        "from pathlib import Path\n\njoin = Path.joinpath\n",
        "import os\n\nsep = os.sep\n",
        "import os\n\ndef split(p: str) -> tuple[str, str]:\n    return os.path.split(p)\n",
        "import inspect\n\ndef signature(f: object) -> object:\n    return inspect.signature(f)\n",
        "def clean(rows: list[str]) -> None:\n    for line in rows:\n        line.replace('a', 'b')\n",
        "def first(match: object) -> object:\n    return match.group(1)\n",
        "from pathlib import Path\n\ndef f(p: Path) -> Path:\n    return p.joinpath('a')\n",
        "def f(box: object) -> object:\n    return box.stat\n",
    ],
)
def test_a_pure_look_alike_is_not_an_impure_call(make_repo: RepoFactory, source: str) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/a.py": source,
            "pkg/meizel.toml": 'purity = "strict"\n',
        }
    )
    assert diagnostics(root, "impure-call") == []


def test_a_shell_constructor_the_policy_lists_is_an_impure_call(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "meizel.policy.toml": render_policy({}, {})
            + '\n[purity]\nimpure-calls = ["meizel.graph.universe.GitRepo"]\n',
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "from meizel.graph.universe import GitRepo\n\n"
                "def repo(root: object) -> object:\n    return GitRepo(root)\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    keys = [d.debt.key for d in diagnostics(root, "impure-call") if d.debt is not None]
    assert keys == ["a.py#meizel.graph.universe.GitRepo"]


@pytest.mark.parametrize(
    "source",
    [
        "from meizel.graph.universe import GitRepo\n\nmake = GitRepo\n",
        "from meizel.graph.universe import load_universe\n\nload = load_universe\n",
        "from meizel.graph.universe import GitRepo\n\nR = GitRepo\n\ndef f(root: str) -> object:\n    return R(root)\n",
        "from functools import partial\nfrom meizel.graph.universe import GitRepo\n\nmake = partial(GitRepo)\n",
    ],
)
def test_a_policy_listed_constructor_named_without_a_call_is_an_impure_call(
    make_repo: RepoFactory, source: str
) -> None:
    root = make_repo(
        {
            "meizel.policy.toml": render_policy({}, {})
            + '\n[purity]\nimpure-calls = ["meizel.graph.universe.GitRepo", "meizel.graph.universe.load_universe"]\n',
            "pkg/__init__.py": "",
            "pkg/a.py": source,
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    found = diagnostics(root, "impure-call")
    assert found
    assert all(
        d.debt is not None and d.debt.key.startswith("a.py#meizel.graph.universe.") for d in found
    )


def test_a_shell_constructor_named_in_an_annotation_is_not_an_impure_call(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "meizel.policy.toml": render_policy({}, {})
            + '\n[purity]\nimpure-calls = ["meizel.graph.universe.GitRepo"]\n',
            "pkg/__init__.py": "",
            "pkg/a.py": (
                "from meizel.graph.universe import GitRepo\n\n"
                "def root_of(repo: GitRepo) -> object:\n    return repo.root\n"
            ),
            "pkg/meizel.toml": 'purity = "strict-to-be"\n',
        }
    )
    assert diagnostics(root, "impure-call") == []
