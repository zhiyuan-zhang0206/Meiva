# meiva:note
# The temporary-space door: a directory is minted where the space says and removed when the context
# exits, a caller's own directory spares the space's root, and the fake a test hands in records what
# it minted and leaves nothing behind.
from __future__ import annotations

from pathlib import Path

from base.paths.scratch import TempFileScratch
from base.paths.tests.fake_scratch import FakeScratch


def test_a_minted_directory_is_named_by_the_prefix_and_gone_after_the_block() -> None:
    space = TempFileScratch()

    with space.make(prefix="meiva-door-test-") as directory:
        assert directory.parent == space.root
        assert directory.name.startswith("meiva-door-test-")
        assert directory.is_dir()
        holder = directory / "held.txt"
        _ = holder.write_text("written", encoding="utf-8")

    assert not directory.exists()


def test_a_caller_that_names_a_parent_gets_its_directory_there(tmp_path: Path) -> None:
    space = TempFileScratch()

    with space.make(prefix="stage-", parent=tmp_path) as directory:
        assert directory.parent == tmp_path
        assert directory.is_dir()

    assert not directory.exists()


def test_the_fake_mints_under_its_own_root_and_records_each_directory(tmp_path: Path) -> None:
    scratch = FakeScratch(tmp_path / "scratch")

    with scratch.make(prefix="one-") as first:
        with scratch.make(prefix="two-") as second:
            assert first.parent == scratch.root
            assert first != second
            assert first.is_dir() and second.is_dir()
        assert str(second) in {str(path) for path in scratch.minted}
        assert not second.exists()

    assert [path.name for path in scratch.minted] == ["one-1", "two-2"]
    assert not first.exists()


def test_the_fake_honours_a_parent_a_caller_names(tmp_path: Path) -> None:
    scratch = FakeScratch(tmp_path / "scratch")
    parent = tmp_path / "target"
    parent.mkdir()

    with scratch.make(prefix="stage-", parent=parent) as directory:
        assert directory.parent == parent
        assert directory.is_dir()

    assert not directory.exists()
