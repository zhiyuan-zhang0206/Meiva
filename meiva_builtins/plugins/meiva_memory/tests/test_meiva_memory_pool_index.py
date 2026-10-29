from __future__ import annotations

from pathlib import Path

from meiva_builtins.plugins.meiva_memory.pool_ops import generate_directory_indexes

# meiva:note
# Git cannot store an empty directory, so the only note-free directory a pool can hold is a
# working-tree artifact: `__pycache__` left by importing the module under test, an editor scratch
# dir. Such a directory used to appear as "0 notes" in `index.md` -- the one place where this
# generator and the git-tree one disagreed on the same pool.
# meiva:link meiva_builtins/plugins/meiva_memory/pool_ops.py


def _note(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\ntitle: {title}\ndescription: {title}\n---\n\n# {title}\n", encoding="utf-8"
    )


def test_a_subdirectory_holding_no_note_is_not_listed(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    _note(pool / "area" / "note.md", "Area note")
    (pool / "area" / "__pycache__").mkdir()
    (pool / "area" / "__pycache__" / "pool_ops.cpython-312.pyc").write_bytes(b"\x00")

    generate_directory_indexes(pool)

    root = (pool / "index.md").read_text(encoding="utf-8")
    area = (pool / "area" / "index.md").read_text(encoding="utf-8")
    assert "* [area/](area/) - 1 notes" in root
    assert "__pycache__" not in area
    assert "* [Area note](note.md) - Area note" in area


def test_a_subdirectory_whose_notes_are_only_deeper_is_still_listed(tmp_path: Path) -> None:
    pool = tmp_path / "pool"
    _note(pool / "area" / "inner" / "note.md", "Inner note")

    generate_directory_indexes(pool)

    root = (pool / "index.md").read_text(encoding="utf-8")
    area = (pool / "area" / "index.md").read_text(encoding="utf-8")
    assert "* [area/](area/) - 1 notes" in root
    assert "* [inner/](inner/) - 1 notes" in area
