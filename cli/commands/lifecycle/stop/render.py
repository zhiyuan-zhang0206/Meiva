# meiva:note
# The words a stop prints around its plan; the shell prints what these return.

from __future__ import annotations

from collections.abc import Sequence

from cli.commands.lifecycle.stop.steps import DataPlane, StopScope

__all__ = [
    "completion_notes",
    "data_plane_skip_line",
    "incomplete_report",
    "phase_timing",
    "preview_lines",
]


def _plane_header(plane: DataPlane) -> str:
    match plane:
        case DataPlane.NONE:
            return "none on this host"
        case DataPlane.KEPT:
            return "retained"
        case DataPlane.STOPPED:
            return "stopped"


def _plane_preview(plane: DataPlane) -> str:
    match plane:
        case DataPlane.NONE:
            return "  infra (pg/redis): skipped (agent-runner uses the central node)"
        case DataPlane.KEPT:
            return "  infra (pg/redis): kept up (cmd_update will migrate next and needs DB)"
        case DataPlane.STOPPED:
            return (
                "  infra (pg/redis): this cluster's own instance stopped (data preserved on disk)"
            )


# meiva:note
# What the stop prints before its confirm gate.
#
# A normal stop prints one header line. A force stop lists the service sessions it will
# stop (`service_sessions`, read from the live root) and what it keeps.
def preview_lines(
    scope: StopScope, service_sessions: Sequence[str], browser_session: str
) -> tuple[str, ...]:
    if not scope.force:
        shells = "retained" if scope.shells_kept else "closed"
        return (
            "[meiva stop] local services; "
            f"shell_sessions={shells}; data plane={_plane_header(scope.data_plane)}",
        )
    listed = ", ".join(service_sessions) if service_sessions else "(none)"
    lines = ["", "The following will be stopped:", f"  service sessions: {listed}"]
    if not scope.shells_kept:
        lines.append("  shell sessions: closed")
    if scope.browser_kept:
        lines.append(f"  browser: kept up ({browser_session}, login session preserved)")
    lines.extend([_plane_preview(scope.data_plane), ""])
    return tuple(lines)


# meiva:note
# What a force stop says when it leaves the data plane alone.
def data_plane_skip_line(plane: DataPlane) -> str:
    if plane is DataPlane.NONE:
        return "\n→ stop pg/redis: skipped (agent-runner uses the central node)"
    return "\n→ stop pg/redis: kept up (keep_infra — migrate/start needs DB)"


# meiva:note
# One stderr line for each shutdown the stop had to escalate.
def completion_notes(notes: Sequence[str]) -> tuple[str, ...]:
    return tuple(f"Stop completed with an escalation: {note}" for note in notes)


# meiva:note
# Each timed phase and the seconds it took, as the report and the journal both spell it.
def phase_timing(phases: Sequence[tuple[str, float]]) -> str:
    return "; ".join(f"{label} {elapsed:.1f}s" for label, elapsed in phases)


# meiva:note
# The stop's real failure with its per-phase budget accounting.
#
# A failed stop names each phase and how much of the shared budget it consumed, so the
# operator sees where the budget went (services vs shell sessions vs data plane), never a
# bare timeout. Nothing is restarted behind the operator's back. `journal_path` is None when the
# stop failed before it opened a lifecycle journal: there is no journal to point at.
def incomplete_report(
    error: str, phases: Sequence[tuple[str, float]], journal_path: str | None
) -> str:
    timing = phase_timing(phases)
    journal = "" if journal_path is None else f" Status journal: {journal_path}."
    return (
        f"Stop incomplete; services and the data plane were not force-killed: {error}. "
        f"phases: {timing or 'before the first phase'}. "
        "Retry the command, or use meiva start (with this unit's explicit service selection) "
        f"to resume.{journal}"
    )
