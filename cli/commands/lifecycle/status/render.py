# meiva:note
# The fixed lines of `meiva status`: banner, converge marker, host reading, drift, release.
#
# Values in, lines out: the shell reads the facts and prints what this module returns. A
# blank line is an empty string in the returned tuple.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "HostReading",
    "HostReadingFailed",
    "banner_lines",
    "converge_lines",
    "host_resource_lines",
    "release_line",
    "source_drift_lines",
]

_DETACHED = "HEAD"


# meiva:note
# One live CPU / memory / disk reading.
@dataclass(frozen=True)
class HostReading:
    cpu_pct: float
    mem_pct: float
    mem_used_gb: float
    mem_total_gb: float
    disk_pct: float
    disk_used_gb: float
    disk_total_gb: float


# meiva:note
# The reading could not be taken (psutil absent on this host); `reason` says why.
@dataclass(frozen=True)
class HostReadingFailed:
    reason: str


# meiva:note
# The first line names the checkout and the capability set; None or empty is unknown.
def banner_lines(repo: str, roles: frozenset[str] | None) -> tuple[str, ...]:
    label = ",".join(sorted(roles)) if roles else "unknown"
    return (f"[meiva status] cwd = {repo}  roles = {label}", "")


# meiva:note
# A converge that aborted leaves a marker in this home; print it first,
# because the services may be up while one step never applied.
# meiva:link cli/commands/converge/docs/converge-incomplete.meiva.okf.md
def converge_lines(summary: str | None) -> tuple[str, ...]:
    return () if summary is None else (f"converge: INCOMPLETE {summary}",)


# meiva:note
# ONE live CPU / memory / disk reading, taken straight from psutil and never from the
# observability stack: this is precisely the answer that must survive an LGTM backend that
# is down or was never deployed. The HISTORY lives in Prometheus (issue #46, the Grafana
# `meiva-ops-main` dashboard, "Host & data plane" section); nothing here retains a series.
# A failed reading is its own line, so losing it never costs the operator the rest.
def host_resource_lines(reading: HostReading | HostReadingFailed) -> tuple[str, ...]:
    if isinstance(reading, HostReadingFailed):
        return (f"  unavailable ({reading.reason})",)
    return (
        f"  cpu {reading.cpu_pct:.0f}%   "
        f"memory {reading.mem_pct:.0f}% ({reading.mem_used_gb:.1f}/{reading.mem_total_gb:.1f} GB)   "
        f"disk {reading.disk_pct:.0f}% ({reading.disk_used_gb:.0f}/{reading.disk_total_gb:.0f} GB)",
    )


# meiva:note
# The prod source ($MEIVA_HOME/source) branch state, on any installed host. A source-run
# home executes that checkout. Source-mode releases materialize as a detached HEAD at the
# released commit (fleet_update switches every checkout to `--detach NEW`), so a detached
# tree is the designed steady state, not drift, and `checkout main` would diverge this host
# from the fleet release. A feature branch here means someone developed in the prod tree
# instead of a worktree: un-reviewed code on the next restart.
def source_drift_lines(branch: str | None) -> tuple[str, ...]:
    if branch is None:
        return ()
    if branch == _DETACHED:
        return (
            "",
            "· prod source ($MEIVA_HOME/source) is detached at the released commit "
            "(source-mode release state — expected; do not `checkout main`, which "
            "would diverge this host from the fleet release).",
        )
    return (
        "",
        f"⚠ prod source ($MEIVA_HOME/source) is on branch '{branch}', not "
        "`main`. A source-run home executes this tree; a feature branch here runs "
        "un-reviewed code on the next restart.",
        "   Develop in a worktree, never the prod checkout. Recover: stash / branch "
        "any work, then restore this tree to the fleet's released commit (ask the "
        "operator / re-run the fleet update) — do not `checkout main`.",
    )


# meiva:note
# The checkout this home executes; an unreadable HEAD prints as unreadable.
def release_line(repo: str, head: str | None) -> str:
    return f"release: source checkout {repo} at {head[:7] if head else 'unreadable HEAD'}"
