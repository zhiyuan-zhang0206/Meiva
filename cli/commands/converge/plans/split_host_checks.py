# meiva:note
# The checks that apply only to an agent-runner split from its gateway.
#
# A single box (a unit that also carries the `gateway` capability) has no peer to transfer
# files to and consolidates memory locally with no PR round-trip, so both checks are skipped
# there. Pure gates and messages; the shells probe the host and print or raise.

from __future__ import annotations

__all__ = [
    "drive_unavailable_warning",
    "github_pr_failure",
    "probes_cross_machine_transfer",
    "requires_github_pr",
]


def _single_box(roles: frozenset[str] | None) -> bool:
    return roles is not None and "gateway" in roles


# meiva:note
# Whether the configured transfer backend is probed at all.
#
# `MEIVA_CROSS_MACHINE_TRANSFER_BACKEND=none` skips the probe entirely.
def probes_cross_machine_transfer(*, roles: frozenset[str] | None, backend: str) -> bool:
    return not _single_box(roles) and backend != "none"


# meiva:note
# Whether this host must be able to open and merge PRs on the memory repo.
#
# Skipped on a single box, when the memory pool is local-only (`MEIVA_MEMORY_KEEP_LOCAL`)
# and when a split agent-runner that does not consolidate via PRs opts out with
# `MEIVA_REQUIRE_GITHUB_PR=false`.
def requires_github_pr(*, roles: frozenset[str] | None, keep_local: bool, require_pr: bool) -> bool:
    return not _single_box(roles) and not keep_local and require_pr


def drive_unavailable_warning(candidates: tuple[str, ...]) -> str:
    looked = ", ".join(candidates) or "(no candidate paths)"
    return (
        "  ! cross-machine transfer backend 'drive' is unavailable on this"
        " agent-runner: no writable Google Drive synced folder, so file transfer"
        " via the shared Drive folder will not work. Install Google Drive for"
        " Desktop (macOS), sign in, and make sure the synced 'My Drive' area"
        " is writable to enable it; set MEIVA_CROSS_MACHINE_TRANSFER_BACKEND=none"
        " to skip this probe on a runner that moves files its own way."
        " Looked in: " + looked
    )


def github_pr_failure(reason: str) -> str:
    return (
        f"this agent-runner cannot open+merge GitHub PRs on the memory repo: {reason}. "
        "The memory pool is consolidated nightly by agents that push their machine "
        "branch and open/merge PRs: install the GitHub CLI (`gh`), run `gh auth login`, "
        "and grant the account write access to the memory repo, then retry."
    )
