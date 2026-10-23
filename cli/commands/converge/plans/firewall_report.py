# meiva:note
# What the firewall step and the `meiva firewall` verbs tell the operator.
#
# Pure text over the paths and counts the shells collected: the converge step's summary lines, the
# verb's per-rule lines, the exact manual commands for binaries no direct mutation could allow,
# and the tail of the status report. The shells choose the stream and print.

from __future__ import annotations

__all__ = [
    "converge_summary_lines",
    "missing_rules_lines",
    "status_tail_lines",
    "sync_summary_lines",
]


# meiva:note
# The converge step's report of one sync: a line per action, then the manual fallback.
def converge_summary_lines(
    *, removed: int, allowed: int, failed: tuple[str, ...], total: int, tool: str
) -> tuple[str, ...]:
    lines: list[str] = []
    if removed:
        lines.append(f"  · firewall: removed {removed} stale allow rules")
    if allowed:
        lines.append(f"  · firewall: allowed {allowed} binaries (no popup on their next bind)")
    if failed:
        lines.extend(missing_rules_lines(failed, total, tool))
    return tuple(lines)


# meiva:note
# `meiva firewall sync`'s per-rule lines; 'already converged' when nothing changed or failed.
def sync_summary_lines(
    removed: tuple[str, ...], allowed: tuple[str, ...], *, failed: int
) -> tuple[str, ...]:
    changes = (
        *(f"  · removed stale rule {path}" for path in removed),
        *(f"  · allowed {path}" for path in allowed),
    )
    if not changes and not failed:
        return ("  already converged — nothing to do",)
    return changes


# meiva:note
# The historical fallback: name the binaries and print the exact commands.
#
# Reached when direct mutation and the older-macOS `sudo -n` fallback both failed.
def missing_rules_lines(missing: tuple[str, ...], total: int, tool: str) -> tuple[str, ...]:
    return (
        f"  ! firewall: {len(missing)} of {total} managed binaries have no ALF "
        "allow rule, so off-box connections to them are dropped while loopback "
        "still works",
        *(f"    - no allow rule: {path}" for path in missing),
        "    left unfixed, off-box peers cannot reach these services (the gateway "
        "serves loopback, no runner can reach it)",
        "    fix (this host rejected both direct mutation and the older macOS "
        "`sudo -n` fallback; paste the commands below):",
        "    note: if the binary shares its bundle identifier with an already "
        "allow-listed app (every uv interpreter is identifier `-`, adb is `adb`), "
        "macOS 15's daemon accepts `sudo socketfilterfw --add` and persists "
        "nothing — allow the popup on the app's first bind instead, or remove "
        "the older rule for the same identifier first.",
        *(
            f'      sudo "{tool}" {verb} "{path}"'
            for path in missing
            for verb in ("--add", "--unblockapp")
        ),
        "    then restart the affected service so it re-binds under the new rule "
        "(an already-bound socket keeps the policy it was accepted under)",
    )


# meiva:note
# The end of `meiva firewall status`: the stale rules and whether the sudo grant exists.
def status_tail_lines(stale: tuple[str, ...], *, sudo_grant: bool) -> tuple[str, ...]:
    grant = (
        "  sudo fallback grant: installed (used only if direct mutation is rejected)"
        if sudo_grant
        else "  sudo fallback grant: not installed (direct mutation was verified on "
        "the macmini running macOS 15.3.1; "
        "sync prints manual commands if this host requires elevation)"
    )
    return (f"  stale rules: {len(stale)}", *(f"    - {path}" for path in stale), grant)
