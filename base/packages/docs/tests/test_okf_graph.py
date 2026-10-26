# meiva:note
# base.packages.docs.okf_graph — the graph builder is a pure function of a path-to-text mapping.

from __future__ import annotations

from base.packages.docs.okf_graph import build_graph_data


def _node(title: str, body: str = "") -> str:
    return f"---\ntype: doc\ntitle: {title}\ndescription: d\ntags: [a]\n---\n\n{body}\n"


def test_a_bundle_given_as_text_has_one_root_tree_edges_and_resolved_cross_edges() -> None:
    raw_links: list[tuple[str, str, str | None]] = []
    files = {
        "docs/index.meiva.okf.md": _node("Index"),
        "pkg/docs/pkg.meiva.okf.md": _node(
            "Pkg", "See [[other/docs/leaf.meiva.okf.md]] and [[nope]]."
        ),
        "pkg/docs/child.meiva.okf.md": _node("Child"),
        "other/docs/leaf.meiva.okf.md": _node("Leaf"),
    }

    data = build_graph_data(files, name="bundle", raw_links_out=raw_links)

    assert data["name"] == "bundle"
    assert [node["id"] for node in data["nodes"]] == sorted(files)
    assert [node["id"] for node in data["nodes"] if node["parent"] is None] == [
        "docs/index.meiva.okf.md"
    ]
    assert {(edge["source"], edge["target"]) for edge in data["treeEdges"]} == {
        ("docs/index.meiva.okf.md", "pkg/docs/pkg.meiva.okf.md"),
        ("pkg/docs/pkg.meiva.okf.md", "pkg/docs/child.meiva.okf.md"),
        ("docs/index.meiva.okf.md", "other/docs/leaf.meiva.okf.md"),
    }
    assert [(edge["source"], edge["target"]) for edge in data["crossEdges"]] == [
        ("pkg/docs/pkg.meiva.okf.md", "other/docs/leaf.meiva.okf.md")
    ]
    assert data["tags"] == ["a"]
    assert ("pkg/docs/pkg.meiva.okf.md", "nope", None) in raw_links


def test_an_empty_bundle_has_no_nodes() -> None:
    data = build_graph_data({})

    assert data["nodes"] == [] and data["treeEdges"] == [] and data["crossEdges"] == []
