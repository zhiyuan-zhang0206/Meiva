from __future__ import annotations

import ast
import json
import sqlite3
from pathlib import Path

import pytest

from meizel.contracts.cli import GitRepo
from meizel.graph import ReachabilityScope, reachability_analysis
from meizel.graph import universe as model
from meizel.graph.graphs import feedback_edges, strongly_connected, unneeded_back_edges
from meizel.graph.model import Code
from meizel.tests.synthetic import RepoFactory, commit, run, write

PYPROJECT = '[tool.pytest.ini_options]\ntestpaths = ["tests", "pkg/**/tests", "app/**/tests", "base/**/tests"]\n'
ROLES = '[roles]\napplication = ["pkg", "app", "base"]\ntests = ["tests"]\n'


def without(graph: dict[str, list[str]], edges: list[tuple[str, str]]) -> dict[str, list[str]]:
    return {s: [t for t in ts if (s, t) not in set(edges)] for s, ts in graph.items()}


def test_an_acyclic_graph_has_no_feedback_edges() -> None:
    graph = {"a": ["b", "c"], "b": ["c"], "c": []}
    assert feedback_edges(graph) == []
    assert not strongly_connected(graph)


def test_removing_the_feedback_edges_leaves_the_graph_acyclic() -> None:
    graph = {
        "a": ["b"],
        "b": ["c", "a"],
        "c": ["d", "a"],
        "d": ["b", "e"],
        "e": [],
    }
    back = feedback_edges(graph)
    assert strongly_connected(graph)
    assert not strongly_connected(without(graph, back))
    assert len(back) <= 2


def test_a_simple_cycle_loses_exactly_one_edge() -> None:
    assert len(feedback_edges({"a": ["b"], "b": ["c"], "c": ["a"]})) == 1


def test_the_choice_is_deterministic() -> None:
    graph = {"x": ["y"], "y": ["z"], "z": ["x"], "w": ["x"], "q": ["w"]}
    assert feedback_edges(graph) == feedback_edges(dict(reversed(list(graph.items()))))


def test_a_recorded_edge_that_closes_no_cycle_is_unneeded() -> None:
    graph = {"a": ["b"], "b": ["c", "a"], "c": []}
    assert unneeded_back_edges(graph, [("b", "a")]) == []
    assert unneeded_back_edges(graph, [("b", "a"), ("b", "c")]) == [("b", "c")]


def test_an_edge_the_graph_no_longer_has_is_unneeded() -> None:
    assert unneeded_back_edges({"a": ["b"], "b": []}, [("b", "a")]) == [("b", "a")]


def test_two_edges_that_each_break_the_cycle_alone_are_not_both_dropped() -> None:
    graph = {"a": ["b"], "b": ["a"]}
    assert unneeded_back_edges(graph, [("a", "b"), ("b", "a")]) == [("a", "b")]


# meiva:note
# The split is a direction: `meizel.graph.universe` holds the data a decision needs,
# `meizel.checkout` is the shell that answers it. These tests pin that direction, so moving the
# reader back into the model — the shape where one file both declared and read a universe — fails
# here as well as in `meizel check`.
# meiva:link meizel/docs/checker/purity.meiva.okf.md
_MODEL = Path(model.__file__).resolve()
_SHELL = _MODEL.parent.parent / "checkout.py"
_EFFECT_MODULES = {"os", "shutil", "subprocess", "tempfile"}
_FILESYSTEM_CALLS = {
    "exists",
    "is_dir",
    "is_file",
    "is_symlink",
    "iterdir",
    "mkdir",
    "read_bytes",
    "read_text",
    "resolve",
    "rglob",
    "rmdir",
    "stat",
    "unlink",
    "walk",
    "write_bytes",
    "write_text",
}
_SHELL_NAMES = {"GitRepo", "apply_plan", "load_universe", "toplevel", "tracked_files"}


def _tree() -> ast.Module:
    return ast.parse(_MODEL.read_text())


def _shell_tree() -> ast.Module:
    return ast.parse(_SHELL.read_text())


def test_the_model_imports_no_effect_module() -> None:
    imported: set[str] = set()
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    assert imported & _EFFECT_MODULES == set()


def test_the_model_calls_no_filesystem_method() -> None:
    called = {
        node.func.attr
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called & _FILESYSTEM_CALLS == set()


def test_the_model_defines_no_shell_name() -> None:
    defined = {
        node.name
        for node in _tree().body
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
    }
    assert defined & _SHELL_NAMES == set()
    assert not hasattr(model, "GitRepo")


def test_the_shell_implements_every_method_the_model_declares() -> None:
    declared = {
        name
        for name in model.Checkout.__dict__
        if not name.startswith("_") and callable(getattr(model.Checkout, name, None))
    }
    assert declared != set()
    shell = next(
        node
        for node in _shell_tree().body
        if isinstance(node, ast.ClassDef) and node.name == "GitRepo"
    )
    implemented = {
        node.name
        for node in shell.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert declared - implemented == set()


def test_the_shell_owns_the_reader() -> None:
    defined = {
        node.name
        for node in _shell_tree().body
        if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef))
    }
    assert defined >= _SHELL_NAMES


def test_derive_lanes_covers_fixtures_autouse_scopes_classes_markers_and_modules(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pyproject.toml": PYPROJECT,
            "meizel.toml": ROLES,
            "base/__init__.py": "",
            "base/deploy/__init__.py": "",
            "base/deploy/tests/__init__.py": "",
            "base/deploy/tests/containers.py": "def start_pg() -> None: pass\n",
            "tests/harness_manifest.py": (
                "from base.deploy.tests.containers import start_pg\n"
                "from pkg.core import VALUE\n"
                "__all__ = ['VALUE', 'start_pg']\n"
            ),
            "tests/test_manifest_contract.py": (
                "from tests.harness_manifest import VALUE\n"
                "def test_expected() -> None:\n    assert VALUE == 42\n"
            ),
            "tests/test_reexported_container.py": (
                "from tests.harness_manifest import start_pg\n"
                "def test_container() -> None:\n    start_pg()\n"
            ),
            "pkg/__init__.py": "",
            "pkg/core.py": "VALUE = 42\n",
            "pkg/tests/shared_fixtures.py": (
                "import pytest\n"
                "@pytest.fixture\n"
                "def seeded_db(db_url: str) -> str:\n    return db_url\n"
            ),
            "pkg/tests/test_pure.py": (
                "from pkg.core import VALUE\ndef test_value() -> None:\n    assert VALUE == 42\n"
            ),
            "pkg/tests/scoped/path_scopes.toml": '"pkg.tests.shared_fixtures" = ["."]\n',
            "pkg/tests/scoped/test_via_scope.py": (
                "def test_seeded(seeded_db: str) -> None:\n    assert seeded_db\n"
            ),
            "pkg/tests/autouse_native/conftest.py": (
                "import pytest\n"
                "@pytest.fixture(autouse=True)\n"
                "def _boot_bus(event_bus: object) -> None:\n    pass\n"
            ),
            "pkg/tests/autouse_native/test_implicit.py": "def test_ok() -> None: pass\n",
            "app/__init__.py": "",
            "app/tests/test_class_method.py": (
                "class TestStore:\n"
                "    def test_save(self, redis_url: str) -> None:\n        assert redis_url\n"
            ),
            "app/tests/test_marked.py": (
                "import pytest\n@pytest.mark.requires_host\ndef test_host() -> None: pass\n"
            ),
            "app/tests/test_dynamic_fixture.py": (
                "import pytest\n"
                "def test_dyn(request: pytest.FixtureRequest, name: str = 'x') -> None:\n"
                "    request.getfixturevalue(name)\n"
            ),
        }
    )
    lanes = GitRepo(root).test_lanes()
    assert lanes.static == frozenset(
        {
            "pkg/tests/test_pure.py",
            "tests/test_manifest_contract.py",
        }
    )
    assert lanes.native == frozenset(
        {
            "app/tests/test_class_method.py",
            "app/tests/test_dynamic_fixture.py",
            "app/tests/test_marked.py",
            "pkg/tests/autouse_native/test_implicit.py",
            "pkg/tests/scoped/test_via_scope.py",
            "tests/test_reexported_container.py",
        }
    )


def test_sqlite_cache_persists_lanes_and_invalidates_on_dirty_or_untracked_edits(
    make_repo: RepoFactory,
) -> None:
    root = make_repo(
        {
            "pyproject.toml": PYPROJECT,
            "meizel.toml": ROLES,
            "pkg/__init__.py": "",
            "pkg/tests/test_unit.py": "def test_unit() -> None: pass\n",
        }
    )
    repo = GitRepo(root)
    first = repo.test_lanes()
    assert first.static == frozenset({"pkg/tests/test_unit.py"})
    assert first.native == frozenset()

    db_path = root / ".git" / "meizel-scan-cache-v1.sqlite3"
    assert db_path.is_file()
    with sqlite3.connect(db_path) as conn:
        keys = [row[0] for row in conn.execute("SELECT k FROM scan_cache").fetchall()]
    assert any(key.startswith("pytest-lanes:v1:") for key in keys)
    assert any(key.startswith("pytest-facts:v1:") for key in keys)
    assert repo.test_lanes() == first

    (root / "pkg/tests/test_unit.py").write_text(
        "def test_unit(db_url: str) -> None:\n    assert db_url\n",
        encoding="utf-8",
    )
    flipped = repo.test_lanes()
    assert flipped.static == frozenset()
    assert flipped.native == frozenset({"pkg/tests/test_unit.py"})

    (root / "pkg/tests/test_untracked.py").write_text(
        "def test_untracked() -> None: pass\n",
        encoding="utf-8",
    )
    with_untracked = repo.test_lanes()
    assert with_untracked.static == frozenset({"pkg/tests/test_untracked.py"})
    assert with_untracked.native == frozenset({"pkg/tests/test_unit.py"})


def test_cli_tests_subcommand_reports_lanes_and_strict_cutoff_selection(
    make_repo: RepoFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = make_repo(
        {
            "pyproject.toml": PYPROJECT,
            "meizel.toml": '[uses]\n"app" = ["pkg"]\n"tests" = ["app", "pkg"]\n\n' + ROLES,
            "pkg/__init__.py": "",
            "pkg/meizel.toml": 'exports = ["calc.py"]\npurity = "strict"\n',
            "pkg/calc.py": "def double(x: int) -> int:\n    return x * 2\n",
            "pkg/tests/test_calc.py": (
                "from pkg.calc import double\n"
                "def test_double() -> None:\n    assert double(3) == 6\n"
            ),
            "app/__init__.py": "",
            "app/tests/test_app_native.py": (
                "from pkg.calc import double\n"
                "def test_with_db(db_url: str) -> None:\n    assert double(2) == 4 and db_url\n"
            ),
            "tests/test_lint_tree.py": "def test_tree() -> None: pass\n",
        }
    )
    commit(root, "initial")

    assert run(root, "tests", "--lane", "static", "--json") == 0
    static_payload = json.loads(capsys.readouterr().out)
    assert static_payload == {
        "count": 2,
        "lane": "static",
        "tests": ["pkg/tests/test_calc.py", "tests/test_lint_tree.py"],
    }

    assert run(root, "tests", "--lane", "native") == 0
    assert capsys.readouterr().out.splitlines() == ["app/tests/test_app_native.py"]

    write(
        root,
        {
            "pkg/calc.py": (
                "def _step(x: int) -> int:\n    return x + x\n\n"
                "def double(x: int) -> int:\n    return _step(x)\n"
            )
        },
    )
    assert run(root, "tests", "--changed", "pkg/calc.py", "--base-ref", "HEAD", "--json") == 0
    cutoff_payload = json.loads(capsys.readouterr().out)
    assert cutoff_payload["decision"] == "SELECTED"
    assert cutoff_payload["tests"] == ["pkg/tests/test_calc.py", "tests/test_lint_tree.py"]

    write(
        root,
        {"pkg/calc.py": "def double(x: int, *, factor: int = 2) -> int:\n    return x * factor\n"},
    )
    assert run(root, "tests", "--changed", "pkg/calc.py", "--base-ref", "HEAD", "--json") == 0
    propagated = json.loads(capsys.readouterr().out)
    assert propagated["tests"] == [
        "app/tests/test_app_native.py",
        "pkg/tests/test_calc.py",
        "tests/test_lint_tree.py",
    ]

    assert (
        run(
            root,
            "tests",
            "--changed",
            "pkg/calc.py",
            "--base-ref",
            "HEAD",
            "--lane",
            "native",
            "--json",
        )
        == 0
    )
    native_only = json.loads(capsys.readouterr().out)
    assert native_only["tests"] == ["app/tests/test_app_native.py"]


def test_reachability_pure_traversal_and_ancestor_inits(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pyproject.toml": PYPROJECT,
            "meizel.toml": ROLES,
            "app/meizel.toml": '[roots]\n"main.py" = { kinds = ["environ"], why = "entry" }\n',
            "app/__init__.py": "from app.helper import boot_helper\n",
            "app/helper.py": "def boot_helper(): pass\n",
            "app/main.py": "import pkg.calc\n",
            "pkg/__init__.py": "",
            "pkg/calc.py": "def add(a, b): return a + b\n",
        }
    )
    commit(root, "initial")

    repo = GitRepo(root)
    universe = repo.universe()
    from meizel.contracts.analysis import analyze

    analysis = analyze(universe)
    report = reachability_analysis(
        universe,
        facts=analysis.facts,
        manifests=analysis.manifests,
        scope=ReachabilityScope(target_roles=("application",)),
    )

    assert report.is_clean
    assert "app/main.py" in report.reachable_production
    assert "app/helper.py" in report.reachable_production
    assert "pkg/calc.py" in report.reachable_production
    assert not report.disconnected_islands
    assert not report.fake_test_islands

    report_with_inits = reachability_analysis(
        universe,
        facts=analysis.facts,
        manifests=analysis.manifests,
        scope=ReachabilityScope(target_roles=("application",), include_inits=True),
    )
    assert "app/__init__.py" in report_with_inits.reachable_production
    assert "pkg/__init__.py" in report_with_inits.reachable_production


def test_reachability_detects_disconnected_islands_and_clusters(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pyproject.toml": PYPROJECT,
            "meizel.toml": ROLES,
            "app/meizel.toml": '[roots]\n"main.py" = { kinds = ["environ"], why = "entry" }\n',
            "app/__init__.py": "",
            "app/main.py": "print('live')\n",
            "pkg/__init__.py": "",
            "pkg/dead_a.py": "import pkg.dead_b\n",
            "pkg/dead_b.py": "import pkg.dead_a\n",
            "pkg/lonely_dead.py": "x = 42\n",
        }
    )
    commit(root, "initial")

    repo = GitRepo(root)
    universe = repo.universe()
    from meizel.contracts.analysis import analyze

    analysis = analyze(universe)
    report = reachability_analysis(
        universe,
        facts=analysis.facts,
        manifests=analysis.manifests,
        scope=ReachabilityScope(target_roles=("application",)),
    )

    assert not report.is_clean
    assert set(report.disconnected_islands) == {
        "pkg/dead_a.py",
        "pkg/dead_b.py",
        "pkg/lonely_dead.py",
    }
    assert not report.fake_test_islands

    cluster_files = [set(c.files) for c in report.clusters]
    assert {"pkg/dead_a.py", "pkg/dead_b.py"} in cluster_files
    assert {"pkg/lonely_dead.py"} in cluster_files

    diags_by_path = {d.path: d for d in report.diagnostics}
    diag_a = diags_by_path["pkg/dead_a.py"]
    assert diag_a.code is Code.DISCONNECTED_ISLAND
    assert "disconnected dead code island" in diag_a.message
    assert any("safely delete this module (GC)" in h for h in diag_a.hints)
    assert any("island cluster (2 files)" in h for h in diag_a.hints)


def test_reachability_distinguishes_fake_test_preserved_islands(make_repo: RepoFactory) -> None:
    root = make_repo(
        {
            "pyproject.toml": PYPROJECT,
            "meizel.toml": ROLES,
            "app/meizel.toml": '[roots]\n"main.py" = { kinds = ["environ"], why = "entry" }\n',
            "app/__init__.py": "",
            "app/main.py": "print('live')\n",
            "pkg/__init__.py": "",
            "pkg/preserved_feature.py": "def feature_logic(): return True\n",
            "pkg/dead_completely.py": "pass\n",
            "tests/test_fake.py": "from pkg.preserved_feature import feature_logic\ndef test_it(): assert feature_logic()\n",
        }
    )
    commit(root, "initial")

    repo = GitRepo(root)
    universe = repo.universe()
    from meizel.contracts.analysis import analyze

    analysis = analyze(universe)
    report = reachability_analysis(
        universe,
        facts=analysis.facts,
        manifests=analysis.manifests,
        scope=ReachabilityScope(target_roles=("application",)),
    )

    assert "pkg/preserved_feature.py" in report.fake_test_islands
    assert "pkg/dead_completely.py" in report.disconnected_islands

    diags_by_path = {d.path: d for d in report.diagnostics}
    fake_diag = diags_by_path["pkg/preserved_feature.py"]
    assert fake_diag.code is Code.FAKE_TEST_ONLY_ISLAND
    assert "preserved only by tests" in fake_diag.message
    assert any("tests/test_fake.py" in h for h in fake_diag.hints)

    dead_diag = diags_by_path["pkg/dead_completely.py"]
    assert dead_diag.code is Code.DISCONNECTED_ISLAND


def test_reachability_cli_execution_and_gating(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            "pyproject.toml": PYPROJECT,
            "meizel.toml": ROLES,
            "app/meizel.toml": '[roots]\n"main.py" = { kinds = ["environ"], why = "entry" }\n',
            "app/__init__.py": "",
            "app/main.py": "print('live')\n",
            "pkg/__init__.py": "",
            "pkg/dead.py": "pass\n",
        }
    )
    commit(root, "initial")

    assert run(root, "reachability") == 0
    out = capsys.readouterr().out
    assert "meizel reachability:" in out
    assert "pkg/dead.py:1: [disconnected-island]" in out

    assert run(root, "reachability", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["roots"] == ["app/main.py"]
    assert payload["disconnected_islands"] == ["pkg/dead.py"]

    assert run(root, "reachability", "pkg/dead.py", "--check") == 1
    assert run(root, "reachability", "app/main.py", "--check") == 0
