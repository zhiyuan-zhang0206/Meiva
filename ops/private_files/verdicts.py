# meiva:note
# What the private-file verifier concludes from the facts it read, as values.
#
# An `active` entry must exist on disk AND match its durable git source (the missing-file
# guard); an `archived` entry's disk copy may be gone, and only its durable source must exist and
# match, so an archived entry stays verifiable forever instead of failing the moment its residue
# is deleted. Any other status is refused loudly. The shell reads the disk and runs git; this
# module only judges what it found.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["GitProbe", "disk_errors", "requires_disk_file", "source_errors"]


# meiva:note
# Whether an entry's status makes the disk copy part of the contract.
def requires_disk_file(status: str) -> bool:
    if status == "active":
        return True
    if status == "archived":
        return False
    raise ValueError(f"unsupported private-files entry status: {status!r}")


# meiva:note
# The errors of a checked file: it is missing, or its digest differs from the registered one.
# `actual_sha256` is None exactly when the file is missing.
def disk_errors(
    *, is_file: bool, actual_sha256: str | None, expected_sha256: str
) -> tuple[str, ...]:
    if not is_file:
        return ("file is missing",)
    if actual_sha256 is None:
        raise ValueError("an existing file needs its digest")
    if actual_sha256 != expected_sha256:
        return (f"file sha256 mismatch: expected {expected_sha256}, got {actual_sha256}",)
    return ()


# meiva:note
# What the shell learned about an entry's durable git source, as far as it needed to look.
#
# `repo_exists` is whether the repository directory is there. `object_exists`, `blob_readable`
# and `blob_sha256` stay None until the step before them succeeded, so a probe that skipped a step
# it should have run is refused instead of read as success.
@dataclass(frozen=True)
class GitProbe:
    repo: str
    object_name: str
    repo_exists: bool
    object_exists: bool | None = None
    blob_readable: bool | None = None
    blob_sha256: str | None = None


# meiva:note
# The first thing wrong with the durable source, or no error: the repository is missing, the
# object is missing, the object cannot be read, or its digest differs from the registered one.
def source_errors(probe: GitProbe, expected_sha256: str) -> tuple[str, ...]:
    if not probe.repo_exists:
        return (f"source git repository is missing: {probe.repo}",)
    if probe.object_exists is None:
        raise ValueError("a probe of an existing repository needs its object lookup")
    if not probe.object_exists:
        return (f"source git object is missing: {probe.repo}@{probe.object_name}",)
    if probe.blob_readable is None:
        raise ValueError("a probe of an existing object needs its read")
    if not probe.blob_readable:
        return (f"source git object cannot be read: {probe.repo}@{probe.object_name}",)
    if probe.blob_sha256 is None:
        raise ValueError("a readable object needs its digest")
    if probe.blob_sha256 != expected_sha256:
        return (f"source sha256 mismatch: expected {expected_sha256}, got {probe.blob_sha256}",)
    return ()
