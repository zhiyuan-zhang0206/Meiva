# meiva:note
# What installing one downloaded native LGTM archive puts where, and how the pinned Loki parser
# is asked to check the rendered config.
#
# The shell in `lgtm_native.py` downloads, hashes, extracts and runs; the layout, the markers it
# leaves and the messages are decided here.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ExtractMember",
    "ExtractTree",
    "checksum_problem",
    "extraction_plan",
    "install_markers",
    "loki_verify_argv",
    "loki_verify_error",
]

# meiva:note
# Where a release tree backend (Grafana) is installed, relative to the native directory.
_TREE_DESTINATION = "grafana-home"


# meiva:note
# Copy one named archive member to a single executable at `destination` (relative to the native
# directory).
@dataclass(frozen=True)
class ExtractMember:
    member: str
    destination: str


# meiva:note
# Install the archive's one top-level directory as `destination` (relative to the native
# directory).
@dataclass(frozen=True)
class ExtractTree:
    destination: str


# meiva:note
# How a backend's archive is installed: a backend with an archive member is one executable, one
# without (`archive_member` None) is a release tree.
#
# The member named by the pinned asset wins over the constant, which is the darwin default.
def extraction_plan(
    *, archive_member: str | None, asset_member: str, binary_path: str
) -> ExtractMember | ExtractTree:
    if archive_member is None:
        return ExtractTree(_TREE_DESTINATION)
    return ExtractMember(asset_member or archive_member, binary_path)


# meiva:note
# The marker files a finished install leaves, as (file name, content) pairs: the version and the
# platform tag the next pass compares against.
def install_markers(name: str, *, version: str, tag: str) -> tuple[tuple[str, str], ...]:
    return ((f"version-{name}", version + "\n"), (f"platform-{name}", tag + "\n"))


# meiva:note
# The refusal for an archive whose SHA-256 differs from the pin, or None when it matches.
def checksum_problem(name: str, version: str, *, actual: str, expected: str) -> str | None:
    if actual == expected:
        return None
    return (
        f"native LGTM {name} {version} SHA256 mismatch: got {actual}, "
        f"expected {expected} — refusing to install"
    )


# meiva:note
# The argv that asks the pinned Loki binary to parse the rendered config under `home`.
def loki_verify_argv(binary: str, home: str) -> tuple[str, ...]:
    return (binary, f"-config.file={home}/lgtm/native/config/loki.yaml", "-verify-config")


# meiva:note
# The error raised when the Loki parser rejects the rendered config.
def loki_verify_error(stderr: str) -> str:
    return f"Loki config verification failed: {stderr.strip()}"
