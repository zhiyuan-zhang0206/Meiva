from __future__ import annotations

from services.backup.scheduler.operation.cleanup_plan import (
    Entry,
    Removal,
    artifact_removals,
    scratch_removal,
)


def _file(name: str) -> Entry:
    return Entry(name, is_file=True, is_dir=False, is_symlink=False)


def test_a_complete_encrypted_artifact_is_all_that_survives() -> None:
    entries = [
        _file("meiva-20260926T000000Z.dump.enc"),
        _file("meiva-20260926T000000Z.dump.partial"),
        _file("meiva-20260926T000000Z.dump.enc.partial"),
        _file("notes.txt"),
        _file("not-a-managed-name.dump.enc"),
    ]

    assert artifact_removals(entries) == [
        Removal("meiva-20260926T000000Z.dump.partial", tree=False),
        Removal("meiva-20260926T000000Z.dump.enc.partial", tree=False),
        Removal("notes.txt", tree=False),
        Removal("not-a-managed-name.dump.enc", tree=False),
    ]


def test_a_directory_goes_as_a_tree_and_a_link_as_a_file() -> None:
    entries = [
        Entry("nested", is_file=False, is_dir=True, is_symlink=False),
        Entry("meiva-20260926T000000Z.dump.enc", is_file=True, is_dir=False, is_symlink=True),
        Entry("linked-dir", is_file=False, is_dir=True, is_symlink=True),
    ]

    assert artifact_removals(entries) == [
        Removal("nested", tree=True),
        Removal("meiva-20260926T000000Z.dump.enc", tree=False),
        Removal("linked-dir", tree=False),
    ]


def test_nothing_staged_removes_nothing() -> None:
    assert artifact_removals([]) == []


def test_only_a_real_scratch_directory_is_removed_as_a_tree() -> None:
    real = Entry("scratch", is_file=False, is_dir=True, is_symlink=False)
    link = Entry("scratch", is_file=False, is_dir=True, is_symlink=True)
    absent = Entry("scratch", is_file=False, is_dir=False, is_symlink=False)
    plain = Entry("scratch", is_file=True, is_dir=False, is_symlink=False)

    assert scratch_removal(real) == Removal("scratch", tree=True)
    assert scratch_removal(link) is None
    assert scratch_removal(absent) is None
    assert scratch_removal(plain) is None
