from __future__ import annotations

import ast
from pathlib import Path

from meizel.graph import universe as model
from meizel.graph.graphs import feedback_edges, strongly_connected, unneeded_back_edges


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
