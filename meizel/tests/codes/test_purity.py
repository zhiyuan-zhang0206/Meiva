from __future__ import annotations

import pytest

from meizel.language.bindings import is_imported_binding, module_bindings
from meizel.tests.synthetic import RepoFactory, analysis_of, diagnostics, git, run


def test_manifest_purity_accepts_top_level_and_table_forms(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/pure.py": "def add(x: int, y: int) -> int:\n    return x + y\n",
            "pkg/impure.py": "import subprocess\n",
            "pkg/meizel.toml": '[purity]\n"." = "none"\n"pure.py" = "strict"\n',
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
    keys = sorted(d.debt.key for d in diagnostics(root, "unfrozen-value") if d.debt is not None)
    assert keys == ["a.py#class AlsoBad", "a.py#class Bad"]


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
    b = module_bindings(root, "lib/consumer.py")
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
