from __future__ import annotations

import pytest

from ops.private_files.verdicts import GitProbe, disk_errors, requires_disk_file, source_errors

_SHA = "a" * 64
_OTHER = "b" * 64


# meiva:note
# An active entry requires its disk copy; an archived one is certified by its durable source alone.
def test_only_an_active_entry_requires_its_disk_copy() -> None:
    assert requires_disk_file("active") is True
    assert requires_disk_file("archived") is False


# meiva:note
# An unexpected status is refused loudly, never treated as one of the two.
@pytest.mark.parametrize("status", ["", "Active", "retired", "deleted"])
def test_an_unsupported_status_is_refused(status: str) -> None:
    with pytest.raises(ValueError, match="unsupported private-files entry status"):
        requires_disk_file(status)


# meiva:note
# A present file with the registered digest has no error.
def test_a_matching_file_has_no_error() -> None:
    assert disk_errors(is_file=True, actual_sha256=_SHA, expected_sha256=_SHA) == ()


# meiva:note
# A missing file and a tampered one are different errors.
def test_a_missing_or_tampered_file_is_named() -> None:
    assert disk_errors(is_file=False, actual_sha256=None, expected_sha256=_SHA) == (
        "file is missing",
    )
    assert disk_errors(is_file=True, actual_sha256=_OTHER, expected_sha256=_SHA) == (
        f"file sha256 mismatch: expected {_SHA}, got {_OTHER}",
    )


# meiva:note
# A file that exists but arrives without a digest is a caller bug, not a pass.
def test_an_existing_file_without_a_digest_is_refused() -> None:
    with pytest.raises(ValueError, match="needs its digest"):
        disk_errors(is_file=True, actual_sha256=None, expected_sha256=_SHA)


# meiva:note
# The source verdict names the first thing wrong, in the order git is probed: the repository,
# the object, its bytes, then its digest.
def test_the_first_failing_source_step_is_named() -> None:
    repo, name = "/archive", "HEAD:ops/x.py"

    assert source_errors(GitProbe(repo, name, repo_exists=False), _SHA) == (
        "source git repository is missing: /archive",
    )
    assert source_errors(GitProbe(repo, name, repo_exists=True, object_exists=False), _SHA) == (
        "source git object is missing: /archive@HEAD:ops/x.py",
    )
    unreadable = GitProbe(repo, name, repo_exists=True, object_exists=True, blob_readable=False)
    assert source_errors(unreadable, _SHA) == (
        "source git object cannot be read: /archive@HEAD:ops/x.py",
    )
    different = GitProbe(
        repo, name, repo_exists=True, object_exists=True, blob_readable=True, blob_sha256=_OTHER
    )
    assert source_errors(different, _SHA) == (
        f"source sha256 mismatch: expected {_SHA}, got {_OTHER}",
    )


# meiva:note
# A source that matches has no error.
def test_a_matching_source_has_no_error() -> None:
    probe = GitProbe(
        "/archive",
        "HEAD:ops/x.py",
        repo_exists=True,
        object_exists=True,
        blob_readable=True,
        blob_sha256=_SHA,
    )

    assert source_errors(probe, _SHA) == ()


# meiva:note
# A probe that skipped a step it should have run is a caller bug, not a success.
@pytest.mark.parametrize(
    "probe",
    [
        GitProbe("/r", "HEAD:p", repo_exists=True),
        GitProbe("/r", "HEAD:p", repo_exists=True, object_exists=True),
        GitProbe("/r", "HEAD:p", repo_exists=True, object_exists=True, blob_readable=True),
    ],
)
def test_an_incomplete_probe_is_refused(probe: GitProbe) -> None:
    with pytest.raises(ValueError, match="needs its"):
        source_errors(probe, _SHA)
