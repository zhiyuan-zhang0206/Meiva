# meiva:note
# base.packages.okf_bundle — the shell that reads an OKF bundle directory for the graph.

from __future__ import annotations

from pathlib import Path

import pytest

from base.packages.docs.okf_graph import build_graph_data
from base.packages.okf_bundle import read_bundle

_NODE = "---\ntype: doc\ntitle: {title}\ndescription: d\n---\n\n{body}\n"


def _write(root: Path, rel: str, body: str = "", *, title: str | None = None) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_NODE.format(title=title or rel, body=body), encoding="utf-8")


def test_a_note_larger_than_one_mebibyte_is_read_whole(tmp_path: Path) -> None:
    big = "line of a very long note\n" * 60_000
    assert len(big.encode()) > 1024 * 1024
    _write(tmp_path, "docs/index.meiva.okf.md")
    _write(tmp_path, "pkg/docs/big.meiva.okf.md", big)

    bundle = read_bundle(tmp_path)
    data = build_graph_data(bundle)

    assert set(bundle) == {"docs/index.meiva.okf.md", "pkg/docs/big.meiva.okf.md"}
    assert bundle["pkg/docs/big.meiva.okf.md"].endswith(big + "\n")
    node = next(n for n in data["nodes"] if n["id"] == "pkg/docs/big.meiva.okf.md")
    assert node["body"] == big + "\n"


def test_hidden_directories_are_skipped_except_github(tmp_path: Path) -> None:
    _write(tmp_path, "visible.meiva.okf.md")
    _write(tmp_path, ".github/.github.meiva.okf.md")
    _write(tmp_path, ".hidden/x.meiva.okf.md")
    _write(tmp_path, ".git/y.meiva.okf.md")
    _write(tmp_path, "pkg/.venv/z.meiva.okf.md")

    assert sorted(read_bundle(tmp_path)) == [".github/.github.meiva.okf.md", "visible.meiva.okf.md"]


def test_only_okf_notes_are_read(tmp_path: Path) -> None:
    _write(tmp_path, "note.meiva.okf.md")
    (tmp_path / "README.md").write_text("# not a node\n", encoding="utf-8")

    assert sorted(read_bundle(tmp_path)) == ["note.meiva.okf.md"]


def test_a_note_that_is_not_utf8_fails_loudly_instead_of_vanishing(tmp_path: Path) -> None:
    _write(tmp_path, "ok.meiva.okf.md")
    (tmp_path / "bad.meiva.okf.md").write_bytes(b"---\ntitle: \xff\xfe\n---\n")

    with pytest.raises(UnicodeDecodeError):
        read_bundle(tmp_path)
