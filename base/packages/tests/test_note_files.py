# meiva:note
# base.packages.note_files — the shell that walks and reads a note pool.

from __future__ import annotations

from pathlib import Path
from typing import Any

from base.packages.note_files import walk_notes


class TestWalkNotes:
    def test_walks_sorted_and_parses(self, tmp_path: Path) -> None:
        (tmp_path / "b.md").write_text("---\ntitle: B\n---\n\nb\n", encoding="utf-8")
        (tmp_path / "a.md").write_text("---\ntitle: A\n---\n\na\n", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "c.md").write_text("---\ntitle: C\n---\n\nc\n", encoding="utf-8")

        out = list(walk_notes(tmp_path))
        assert [(p.name, n.rel, n.title) for p, n in out] == [
            ("a.md", "a", "A"),
            ("b.md", "b", "B"),
            ("c.md", "sub/c", "C"),
        ]

    def test_skip_names_and_no_frontmatter(self, tmp_path: Path) -> None:
        (tmp_path / "MEMORY.md").write_text("---\ntitle: Idx\n---\n\n#\n", encoding="utf-8")
        (tmp_path / "plain.md").write_text("# no fm\n", encoding="utf-8")
        (tmp_path / "good.md").write_text("---\ntitle: G\n---\n\ng\n", encoding="utf-8")

        out = list(walk_notes(tmp_path, skip_names=frozenset({"MEMORY.md"})))
        assert [n.rel for _, n in out] == ["good"]

    def test_unreadable_file_warns_and_skips(self, tmp_path: Path) -> None:
        (tmp_path / "x.md").mkdir()
        (tmp_path / "good.md").write_text("---\ntitle: G\n---\n\ng\n", encoding="utf-8")
        warnings: list[str] = []
        out = list(walk_notes(tmp_path, warnings=warnings))
        assert [n.rel for _, n in out] == ["good"]
        assert warnings == ["cannot read x.md"]

    def test_unreadable_file_without_warnings_list_logs_a_warning(
        self, tmp_path: Path, loguru_records: list[dict[str, Any]]
    ) -> None:
        (tmp_path / "x.md").mkdir()
        list(walk_notes(tmp_path))
        assert any(
            "cannot read note x.md" in r["message"] and r["level"].no >= 30 for r in loguru_records
        )

    def test_a_note_larger_than_one_mebibyte_is_read_whole(self, tmp_path: Path) -> None:
        big = "a long line of a note body\n" * 60_000
        assert len(big.encode()) > 1024 * 1024
        (tmp_path / "big.md").write_text(f"---\ntitle: Big\n---\n\n{big}", encoding="utf-8")

        ((_, note),) = list(walk_notes(tmp_path))

        assert note.title == "Big" and note.body.endswith(big)
