from __future__ import annotations

import ast
import pathlib
import textwrap

from meizel.state import ambient_state
from meizel.state.ambient_state.module import Tree
from meizel.state.ambient_state.tests.conftest import SCOPE

_BUNDLE_SOURCE = """
    from base.config.wiring import root_bundle

    @root_bundle
    class GatewayWiring:
        db: object
"""


def _sites(
    root: pathlib.Path, source: str, rel: str = "gateway/routers/thing.py"
) -> dict[str, int]:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(source), encoding="utf-8")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    measured = ambient_state.measure(tree, rel, _tree(root), SCOPE)
    return {key.split("::", 1)[1]: len(lines) for key, lines in measured.items()}


def _define_bundle(root: pathlib.Path) -> None:
    path = root / "gateway/wiring.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(_BUNDLE_SOURCE), encoding="utf-8")


def test_a_parameter_annotation_naming_a_bundle_is_a_site(tmp_path: pathlib.Path) -> None:
    _define_bundle(tmp_path)
    source = "from gateway.wiring import GatewayWiring\n\ndef f(w: GatewayWiring) -> None: ...\n"
    assert _sites(tmp_path, source) == {"bundle-leak:GatewayWiring": 1}


def test_a_return_attribute_and_nested_annotation_count_too(tmp_path: pathlib.Path) -> None:
    _define_bundle(tmp_path)
    source = """
        from gateway.wiring import GatewayWiring as W

        def make() -> W: ...

        class Holder:
            wiring: list[W]
    """
    assert _sites(tmp_path, source) == {"bundle-leak:W": 2}


def test_the_defining_module_may_use_its_own_bundle(tmp_path: pathlib.Path) -> None:
    _define_bundle(tmp_path)
    source = textwrap.dedent(_BUNDLE_SOURCE) + "\n\ndef build() -> GatewayWiring: ...\n"
    assert _sites(tmp_path, source, "gateway/wiring.py") == {}


def test_importing_a_bundle_to_unpack_it_is_not_an_annotation(tmp_path: pathlib.Path) -> None:
    _define_bundle(tmp_path)
    source = "from gateway.wiring import GatewayWiring\n\n\ndef f():\n    return GatewayWiring(db=None)\n"
    assert _sites(tmp_path, source) == {}


def test_a_class_without_the_marker_is_not_a_bundle(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "gateway/plain.py"
    path.parent.mkdir(parents=True)
    path.write_text("class Plain:\n    pass\n", encoding="utf-8")
    source = "from gateway.plain import Plain\n\ndef f(p: Plain) -> None: ...\n"
    assert _sites(tmp_path, source) == {}


# meiva:note
# The files a test wrote under `root`, as the tree the rule is handed.
def _tree(root: pathlib.Path) -> Tree:
    files = {path.relative_to(root).as_posix() for path in root.rglob("*.py")}
    dirs = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()}
    return Tree(files, lambda rel: (root / rel).read_text(encoding="utf-8"), dirs)


def _counting_tree(root: pathlib.Path) -> tuple[Tree, dict[str, int]]:
    reads: dict[str, int] = {}
    files = {path.relative_to(root).as_posix() for path in root.rglob("*.py")}
    dirs = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir()}

    def read(rel: str) -> str | None:
        reads[rel] = reads.get(rel, 0) + 1
        return (root / rel).read_text(encoding="utf-8")

    return Tree(files, read, dirs), reads


def test_every_importer_reads_the_defining_module_once(tmp_path: pathlib.Path) -> None:
    # meiva:note
    # Which bundle classes an import names is answered from the tree's module cache, so a module
    # imported by many files is read and parsed once. Before the cache, a whole-tree run parsed the
    # same module once per importer (2026-10-29).
    _define_bundle(tmp_path)
    importers: list[str] = []
    for index in range(5):
        rel = f"gateway/routers/thing{index}.py"
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "from gateway.wiring import GatewayWiring\n\n\n"
            f"def f{index}(w: GatewayWiring) -> None: ...\n",
            encoding="utf-8",
        )
        importers.append(rel)
    tree, reads = _counting_tree(tmp_path)
    for rel in importers:
        source = (tmp_path / rel).read_text(encoding="utf-8")
        assert ambient_state.measure(ast.parse(source), rel, tree) == {
            f"{rel}::bundle-leak:GatewayWiring": [4]
        }
    assert reads["gateway/wiring.py"] == 1
