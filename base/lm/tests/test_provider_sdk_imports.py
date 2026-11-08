# meiva:note
# The provider SDKs are entered at their root, through `base.lm.compat.provider_sdks`, because a submodule
# import from one thread while another thread executes that package's `__init__` gets the
# half-built package back and fails: four hosted turns dropped on company-mini in 24h (#6538).
# meiva:link base/lm/compat/docs/compat.meiva.okf.md

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator
from pathlib import Path

from base.host.proc import run_bounded

_CHILD = Path(__file__).with_name("_provider_sdk_child.py")
_REPO_ROOT = Path(__file__).resolve().parents[3]
_OWNERS = Path(__file__).resolve().parents[1] / "compat" / "provider_sdks"

# meiva:note
# The directories whose runtime modules reach a provider SDK: the provider plugins and `base/lm`. A
# site outside them is caught by nothing here, so its directory joins this list when provider code
# moves. Tests are out of scope: they run in one process that has already imported what it asserts
# on, and the child above is the one that exercises a cold process.
_SCANNED = ("base/lm", "meiva_builtins/plugins")

# meiva:note
# The packages whose submodule a race can hand to a thread half-built. A root import of the same
# package waits for the import already running instead, which is what the owners do.
_SDKS = ("anthropic", "openai", "google.genai")


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"


def _guarded_nodes(tree: ast.Module) -> set[int]:
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for branch in (node.body, node.orelse):
                for child in ast.walk(ast.Module(body=branch, type_ignores=[])):
                    guarded.add(id(child))
    return guarded


def _submodule_import(node: ast.AST) -> tuple[str, int] | None:
    if isinstance(node, ast.ImportFrom) and node.module is not None:
        found = next((name for name in _SDKS if node.module.startswith(f"{name}.")), None)
        return None if found is None else (node.module, node.lineno)
    if isinstance(node, ast.Import):
        for alias in node.names:
            if any(alias.name.startswith(f"{name}.") for name in _SDKS):
                return alias.name, node.lineno
    return None


def _scanned_files() -> Iterator[Path]:
    for directory in _SCANNED:
        for path in sorted((_REPO_ROOT / directory).rglob("*.py")):
            if _OWNERS in path.parents or "tests" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def _offending_imports() -> list[str]:
    found: list[str] = []
    for path in _scanned_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        guarded = _guarded_nodes(tree)
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            imported = _submodule_import(node)
            if imported is not None:
                module, lineno = imported
                found.append(f"{path.relative_to(_REPO_ROOT)}:{lineno} imports {module}")
    return found


def _run_child(scenario: str) -> None:
    result = run_bounded(
        [sys.executable, str(_CHILD), scenario],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_production_path_enters_its_sdk_through_the_owner() -> None:
    _run_child("routes")


def test_threads_reaching_every_sdk_at_once_all_get_a_complete_package() -> None:
    _run_child("race")


def test_no_provider_module_imports_an_sdk_submodule() -> None:
    assert _offending_imports() == []
