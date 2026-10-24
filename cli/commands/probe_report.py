# meiva:note
# What `meiva status` and `meiva start` say about service probes, as text.
#
# Values in, text out: the shell probes the services and hands over plain values (names,
# marks, elapsed seconds); this module decides the wording. It prints nothing.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "ServiceRow",
    "UnreadyWait",
    "non_critical_unready_text",
    "service_row_line",
    "unready_text",
]


# meiva:note
# One service row of `meiva status`: what root says and what the identity probe found.
#
# `skip_reason` is the capability gate's reason when the service is gated out of this
# host's roster; the marks still reflect real liveness, so a still-running gated session
# reads as live with the reason beside it.
@dataclass(frozen=True)
class ServiceRow:
    name: str
    root_running: bool
    # meiva:note
    # True / False are positive and negative probe evidence; None is missing or
    # unobservable evidence and never reads as either.
    probe_alive: bool | None
    probe_label: str
    probe_detail: str
    skip_reason: str | None


# meiva:note
# What a readiness wait found, as the plain values its summary needs.
#
# `sessions_gone` is the early exit (every unready session was absent, so the bound was
# never spent); otherwise the deadline passed with at least one session alive.
@dataclass(frozen=True)
class UnreadyWait:
    names: tuple[str, ...]
    elapsed_s: float
    sessions_gone: bool


def _probe_mark(*, alive: bool | None) -> str:
    if alive is True:
        return "✓"
    if alive is False:
        return "✗"
    return "·"


# meiva:note
# A gated-out service is shown WITH its reason rather than hidden. A failing probe's
# detail rides on the same line: "down" and "answering, but its home is
# /home/meiva/.meiva" call for completely different actions, and the operator has no other
# place to learn which one they are looking at.
def service_row_line(row: ServiceRow, name_width: int) -> str:
    root_mark = "✓" if row.root_running else "✗"
    suffix = f"   -- skipped: {row.skip_reason}" if row.skip_reason else ""
    if not suffix and row.probe_detail:
        suffix = f"   -- {row.probe_detail}"
    return (
        f"{row.name.ljust(name_width)}  {root_mark}     "
        f"{_probe_mark(alive=row.probe_alive)} ({row.probe_label}){suffix}"
    )


# meiva:note
# The services that never became ready, for the operator reading the same output as the
# status snapshot above it.
#
# The two exits get two different sentences because they send the reader to two different
# places. Naming the bound is only meaningful when the bound was spent: the services are
# alive and still not serving, so waiting longer might help. When every session is gone the
# wait returned in about a second, the bound is irrelevant, and the next question is why the
# process is absent; printing the bound there states an elapsed time the surrounding
# timestamps contradict (the 2026-07-30 rollout post-mortem opened by hunting a timeout that
# was never reached, #1016).
def unready_text(wait: UnreadyWait, timeout_s: float) -> str:
    names = ", ".join(wait.names)
    if wait.sessions_gone:
        return (
            f"\n✗ {len(wait.names)} service(s) not ready after {wait.elapsed_s:.1f}s "
            f"— their sessions are gone: {names}\n"
            f"  They died or were never launched, so the {timeout_s:.0f}s readiness bound was "
            f"not spent and raising it would change nothing;\n"
            f"  read why in $MEIVA_HOME/logs. meiva-root keeps trying to revive them and "
            f"re-running `meiva start` with the same service selection is idempotent."
        )
    return (
        f"\n✗ {len(wait.names)} service(s) never became ready within {timeout_s:.0f}s "
        f"({wait.elapsed_s:.1f}s elapsed): {names}\n"
        f"  Their sessions are running and still not serving — their rows above show the "
        f"failing probe. Every start step itself succeeded,\n"
        f"  so this host is up but incomplete; meiva-root keeps trying to revive them, "
        f"`meiva status` re-checks, and re-running `meiva start` with the same service "
        f"selection is idempotent."
    )


# meiva:note
# Optional capabilities unavailable when core startup finishes: the tier that cannot fail the
# start. The cross must still appear (the tier downgrade is a verdict change, never a
# silence) and it points at the event that was emitted.
def non_critical_unready_text(names: tuple[str, ...]) -> str:
    return (
        f"\n✗ {len(names)} non-critical service(s) currently unavailable: {', '.join(names)}\n"
        f"  They do not fail this start (the readiness gate waits for the critical roster "
        f"only),\n"
        f"  but a service_start_unready event was emitted; meiva-root keeps trying to revive "
        f"them and re-running `meiva start` with the same service selection is idempotent."
    )
