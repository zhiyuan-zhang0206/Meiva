# meiva:note
# What the machine verbs of `meiva cluster` say about the gateway's reply.
#
# Values in, text out. The shell dials the gateway and decodes the reply; this module decides
# the wording of every verdict and prints nothing.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "NoGatewayUrl",
    "PauseReply",
    "ReadTimedOut",
    "RefusedWithStatus",
    "RosterFailure",
    "Unreachable",
    "not_registered_line",
    "pause_lines",
    "resume_lines",
    "roster_failure_line",
    "staging_line",
]


# meiva:note
# The gateway answered 404: no row of the machines registry carries that name.
def not_registered_line(name: str) -> str:
    return f"no machine named {name!r} in the registry"


def staging_line(name: str, *, is_staging: bool) -> str:
    action = "marked staging" if is_staging else "unmarked staging (now a fan-out target)"
    return f"{name}: {action}"


# meiva:note
# What the gateway reports after it pulled a machine out of the cluster.
@dataclass(frozen=True)
class PauseReply:
    paused_at: str
    pause_reason: str
    terminated_agents: int
    force_marked_agents: int
    reassigned_tasks: int


def pause_lines(name: str, reply: PauseReply) -> tuple[str, ...]:
    reason = f" — {reply.pause_reason}" if reply.pause_reason else ""
    return (
        f"{name}: PAUSED (since {reply.paused_at[:19]}){reason}",
        f"  agents terminated via the machine's ops server: {reply.terminated_agents}; "
        f"force-marked in DB (machine unreachable): {reply.force_marked_agents}; "
        f"tasks drained to #405: {reply.reassigned_tasks}",
        "  hidden from roster/probe/fan-out/spawn until resumed: "
        f"`meiva cluster resume {name}` (run on the gateway host)",
    )


# meiva:note
# The resume verdict and the ops checklist for the machine's own side: it is away, and its
# reachable address may have changed while it was out.
def resume_lines(name: str, *, resumed: bool) -> tuple[str, ...]:
    verdict = (
        f"{name}: resumed — probing / roster / fan-out / spawn restored"
        if resumed
        else f"{name}: was not paused (no-op)"
    )
    return (
        verdict,
        f"  machine-side checklist (run ON {name} when it is back online):\n"
        "    1. `meiva start --all-services` (or the machine's own explicit service selection) "
        "on the machine — register_self refreshes its dial URL "
        "(the reachable address may have changed) and clears its stopped_at latch.\n"
        "    2. If its reachable address changed, the gateway's pg_hba must cover the new IP: "
        "add the machine's new IP/CIDR to the comma-separated MEIVA_TRUSTED_CIDRS with "
        "`meiva config set MEIVA_TRUSTED_CIDRS=<ranges>`, then run "
        "`meiva restart --all-services --keep-shell-sessions --keep-browser --keep-extras "
        "--mode smooth --timeout 300` ON THE GATEWAY HOST (its own service selection in place of "
        "--all-services) — its start leg rewrites pg_hba.conf and reloads the retained Postgres.\n"
        "    3. Respawn the agents that lived on it (pause terminated them); "
        "`meiva cluster status` / `meiva.agents.list_machines()` shows it again.",
    )


# meiva:note
# The gateway URL could not even be formed (unset gateway URL).
@dataclass(frozen=True)
class NoGatewayUrl:
    detail: str


# meiva:note
# The gateway did not answer within the read budget. A black-holed machine makes the fresh
# roster read ride that machine's probe up to the gateway's `status_probe_timeout_seconds`
# bound, so silence past the budget is the gateway itself not answering.
@dataclass(frozen=True)
class ReadTimedOut:
    url: str
    budget_s: float
    margin_s: float
    detail: str


# meiva:note
# The connection failed before any reply: refused, reset, unresolvable.
@dataclass(frozen=True)
class Unreachable:
    url: str
    detail: str


# meiva:note
# The gateway replied with a non-2xx status.
@dataclass(frozen=True)
class RefusedWithStatus:
    url: str
    status_code: int
    detail: str


RosterFailure = NoGatewayUrl | ReadTimedOut | Unreachable | RefusedWithStatus


# meiva:note
# One line of stderr per way the roster read can fail: the unreachable-machine case is the
# exact situation an operator runs this command for, so each failure says what it was.
def roster_failure_line(failure: RosterFailure) -> str:
    if isinstance(failure, NoGatewayUrl):
        return f"✗ cannot resolve gateway URL: {failure.detail}"
    if isinstance(failure, ReadTimedOut):
        return (
            f"✗ gateway at {failure.url} did not respond within {failure.budget_s:g}s — the "
            f"fresh read waits out a black-holed machine's probe, bounded by the "
            f"gateway's status_probe_timeout_seconds, and this budget adds "
            f"{failure.margin_s:g}s of slack on top; silence past it is the "
            f"gateway itself not answering: {failure.detail}"
        )
    if isinstance(failure, Unreachable):
        return f"✗ gateway unreachable at {failure.url}: {failure.detail}"
    return f"✗ gateway returned HTTP {failure.status_code} for {failure.url}: {failure.detail}"
