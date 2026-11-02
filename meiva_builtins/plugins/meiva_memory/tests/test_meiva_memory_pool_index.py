from __future__ import annotations

from pathlib import Path

from meiva_builtins.plugins.meiva_memory.pool_ops import (
    generate_directory_indexes,
    rebuild_root_memory_index,
)

# meiva:note
# Line-format owner #2: `skills/scripts/tests/test_rebuild_memory_index.py` pins the whole
# generated file, so a change here must be mirrored there before it reaches CI (2026-11-02).
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


# meiva:note
# `MEMORY.md` is injected into every agent's context on every call, and it is capped at
# 16,000 characters (the pool's pre-commit hook). Its per-directory lines used to append a
# count for every grandchild directory: ~4,800 of the 15,900 characters were a listing that
# every `index.md` already carries, and the total grew with every new directory until the cap
# was minutes away from blocking the daily rebuild. The root index now states a top-level
# directory's size and leaves the listing to that directory's own index.
# meiva:link meiva_builtins/plugins/meiva_memory/pool_ops.py
def test_root_index_states_a_directory_total_without_listing_its_children(
    tmp_path: Path,
) -> None:
    pool = tmp_path / "pool"
    _note(pool / "MEMORY.md", "unused")
    (pool / "MEMORY.md").write_text("# Memory\n\n## Pointers\n\n", encoding="utf-8")
    _note(pool / "area" / "note.md", "Area note")
    _note(pool / "area" / "inner" / "deep.md", "Deep note")
    _note(pool / "area" / "inner2" / "deeper.md", "Deeper note")

    rebuild_root_memory_index(pool)

    root = (pool / "MEMORY.md").read_text(encoding="utf-8")
    assert "- [area/](area/index.md) \u2014 3 notes" in root
    assert "inner(" not in root
    assert "inner2(" not in root
