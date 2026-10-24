from __future__ import annotations

import errno

from services.backup.scheduler.commit_plan import (
    digest_problem,
    dump_result_problem,
    link_route,
    restore_result_problem,
    staged_problem,
)

_NAME = "meiva-20260926T000000Z.dump.enc"


def test_a_dump_result_is_exactly_the_artifact_name_and_its_digest() -> None:
    assert dump_result_problem({"artifact": _NAME, "sha256": "ab" * 32}) is None


def test_a_dump_result_of_another_shape_is_invalid() -> None:
    invalid = "logical backup returned an invalid result"

    assert dump_result_problem({"artifact": _NAME}) == invalid
    assert dump_result_problem({"artifact": _NAME, "sha256": "x", "extra": 1}) == invalid
    assert dump_result_problem({}) == invalid


def test_a_dump_result_that_names_a_path_or_a_non_string_escaped_its_controls() -> None:
    escaped = "logical backup result escaped its controls"

    assert dump_result_problem({"artifact": f"../{_NAME}", "sha256": "x"}) == escaped
    assert dump_result_problem({"artifact": f"sub/{_NAME}", "sha256": "x"}) == escaped
    assert dump_result_problem({"artifact": 7, "sha256": "x"}) == escaped
    assert dump_result_problem({"artifact": _NAME, "sha256": 7}) == escaped


def test_a_restore_result_is_the_one_success_shape() -> None:
    assert restore_result_problem({"restored": True}) is None
    for result in ({"restored": False}, {}, {"restored": True, "extra": 1}, {"ok": True}):
        assert restore_result_problem(result) == "logical restore returned an invalid result"


def test_a_staged_file_must_be_a_managed_regular_artifact() -> None:
    refused = "scheduled backup result is not a managed regular artifact"

    assert staged_problem(_NAME, is_symlink=False, is_file=True) is None
    assert staged_problem(_NAME, is_symlink=True, is_file=True) == refused
    assert staged_problem(_NAME, is_symlink=False, is_file=False) == refused
    assert staged_problem("notes.txt", is_symlink=False, is_file=True) == refused
    assert staged_problem(f"{_NAME}.partial", is_symlink=False, is_file=True) == refused


def test_a_digest_that_changed_after_preparation_is_refused() -> None:
    assert digest_problem("ab", "ab") is None
    assert digest_problem("ab", "cd") == "scheduled backup changed after preparation"


def test_only_a_cross_device_link_falls_back_to_a_verified_copy() -> None:
    assert link_route(errno.EXDEV) == "copy"
    for number in (errno.EEXIST, errno.EACCES, errno.ENOSPC, None):
        assert link_route(number) == "raise"
