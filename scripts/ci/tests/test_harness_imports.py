from __future__ import annotations

import ast
from pathlib import Path

from scripts.ci import test_selector
from scripts.ci.test_impact import module_files
from scripts.ci.tests import harness_imports
from scripts.structure.placement import CODE_TOPS, ModuleIndex

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIST = "scripts/ci/tests/harness_imports.py"


# meiva:note
# The Python files every pytest process loads before any test runs: the root conftest, the modules
# it lists in `pytest_plugins` with their package initializers, and every tests/ module they import
# in turn. The selector treats all of them as global, so it does not count their production imports
# against each test; this test is where those imports are checked.
# meiva:link docs/conventions/engineering/test-selection.md
def _harness_files() -> list[str]:
    index = ModuleIndex(_REPO_ROOT)
    pending = ["conftest.py", *test_selector.load_checkout(_REPO_ROOT).global_files]
    seen: set[str] = set()
    while pending:
        file = pending.pop()
        if file in seen:
            continue
        seen.add(file)
        tree = ast.parse((_REPO_ROOT / file).read_text(encoding="utf-8"), filename=file)
        for node in ast.walk(tree):
            modules = [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules = [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
            for module in modules:
                if module.split(".")[0] == "tests":
                    pending.extend(module_files(module, index))
    return sorted(seen)


# meiva:note
# Every first-party production import in the files, at any depth, as (module, imported names) with
# the files that make it. Imports under `if TYPE_CHECKING:` run in no process and are skipped.
def _production_imports(files: list[str]) -> dict[tuple[str, tuple[str, ...]], set[str]]:
    found: dict[tuple[str, tuple[str, ...]], set[str]] = {}
    for file in files:
        tree = ast.parse((_REPO_ROOT / file).read_text(encoding="utf-8"), filename=file)
        skipped = _type_checking_nodes(tree)
        for node in ast.walk(tree):
            if id(node) not in skipped:
                for statement in _production_statement(node):
                    found.setdefault(statement, set()).add(file)
    return found


def _type_checking_nodes(tree: ast.Module) -> set[int]:
    return {
        id(inner)
        for node in ast.walk(tree)
        if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.unparse(node.test)
        for statement in node.body
        for inner in ast.walk(statement)
    }


def _production_statement(node: ast.AST) -> list[tuple[str, tuple[str, ...]]]:
    if isinstance(node, ast.Import):
        return [(alias.name, ()) for alias in node.names if _is_production(alias.name)]
    if (
        isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module
        and _is_production(node.module)
    ):
        return [(node.module, tuple(sorted(alias.name for alias in node.names)))]
    return []


def _is_production(module: str) -> bool:
    return module.split(".", maxsplit=1)[0] in CODE_TOPS


# meiva:note
# The selector does not count the harness's production closure against each test, so a change
# there that breaks what the root conftest and tests/fixtures import would otherwise reach no
# subset. harness_imports.py imports every one of those modules and names with static imports, so
# a broken one fails this test's collection; this test keeps that list equal to what the harness
# really imports. It is pinned into every selected subset (`TREE_SCAN_TESTS`) and runs in the
# static lane on every backend change, and the merge queue's full run is the backstop for behavior.
# meiva:link docs/conventions/engineering/test-selection.md
def test_the_harness_import_list_is_what_the_harness_imports() -> None:
    actual = _pairs(_production_imports(_harness_files()))
    listed = _pairs(_production_imports([_LIST]))
    assert actual
    assert sorted(actual - listed) == [], "add these harness imports to harness_imports.py"
    assert sorted(listed - actual) == [], "remove these from harness_imports.py"
    assert harness_imports.__all__


def _pairs(imports: dict[tuple[str, tuple[str, ...]], set[str]]) -> set[tuple[str, str]]:
    return {(module, name) for module, names in imports for name in names or ("",)}
