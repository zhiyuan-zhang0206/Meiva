# meiva:note
# Which sections `meiva status` prints, in which order.
#
# The shell collects a few facts about this host (`StatusFacts`) and executes the returned
# steps one by one: a `PrintLines` is text already decided, every other step names a probe
# or reading the shell performs at that point and renders with the module that owns its
# wording. Sections that read slow or fallible things (the data plane, the gateway) are
# steps, not facts, so the operator sees the earlier sections while they run.

from __future__ import annotations

from dataclasses import dataclass

from cli.commands.lifecycle.status.render import banner_lines
from cli.commands.lifecycle.status.roster import RosterEntry, ServiceTable, service_table

__all__ = [
    "PrintLines",
    "PrintServiceRows",
    "ProbeDataPlane",
    "ProbeLgtm",
    "ProbeRedisBridge",
    "ReadGateway",
    "ReadHostResources",
    "ReadRelease",
    "ReadSourceDrift",
    "ReportConverge",
    "StatusFacts",
    "StatusStep",
    "plan_status",
]

_INFRA = "infra (pg/redis):"
_INFRA_SKIPPED = "infra (pg/redis): skipped (agent-runner uses central node)"
_HOST = "host (live cpu/memory/disk):"
_LGTM = "lgtm (observability backend):"
_REDIS_BRIDGE = "redis bridge (private-network ingress):"


# meiva:note
# What the shell knows about this host before the first line prints.
@dataclass(frozen=True)
class StatusFacts:
    repo: str
    # meiva:note
    # The capability set, or None when this host's role is not resolvable yet.
    roles: frozenset[str] | None
    roster: tuple[RosterEntry, ...]
    # meiva:note
    # The services the last `meiva start` put in the root manifest, or None before any start.
    launched: frozenset[str] | None
    # meiva:note
    # The host the operator designated for the observability backend.
    lgtm_host: bool


@dataclass(frozen=True)
class PrintLines:
    lines: tuple[str, ...]


# meiva:note
# Print the aborted-converge marker of this home, if it has one.
@dataclass(frozen=True)
class ReportConverge: ...


# meiva:note
# Print the service table: heading, then one probed row per roster entry.
@dataclass(frozen=True)
class PrintServiceRows:
    table: ServiceTable


# meiva:note
# Print this cluster's own pg/redis reachability.
@dataclass(frozen=True)
class ProbeDataPlane: ...


# meiva:note
# Print one live CPU / memory / disk reading.
@dataclass(frozen=True)
class ReadHostResources: ...


# meiva:note
# Print the observability backends' readiness.
@dataclass(frozen=True)
class ProbeLgtm: ...


# meiva:note
# Print the private-network relay's end-to-end PING.
@dataclass(frozen=True)
class ProbeRedisBridge: ...


# meiva:note
# Print the prod source checkout's branch state, when it is not on `main`.
@dataclass(frozen=True)
class ReadSourceDrift: ...


# meiva:note
# Print the commit the source checkout of `repo` is at.
@dataclass(frozen=True)
class ReadRelease:
    repo: str


# meiva:note
# Print the gateway's own cluster-status snapshot, or why there is none.
@dataclass(frozen=True)
class ReadGateway: ...


StatusStep = (
    PrintLines
    | ReportConverge
    | PrintServiceRows
    | ProbeDataPlane
    | ReadHostResources
    | ProbeLgtm
    | ProbeRedisBridge
    | ReadSourceDrift
    | ReadRelease
    | ReadGateway
)


# meiva:note
# The sections for this host. A pure agent-runner has no local pg/redis and no relay
# to report; the host reading and the release always show; the lgtm section shows only
# on the designated host (a host singleton, so the marker, not the role, decides).
def plan_status(facts: StatusFacts) -> tuple[StatusStep, ...]:
    runner_only = (
        facts.roles is not None and "agent-runner" in facts.roles and "gateway" not in facts.roles
    )
    steps: list[StatusStep] = [
        PrintLines(banner_lines(facts.repo, facts.roles)),
        ReportConverge(),
        PrintLines(("",)),
        PrintServiceRows(service_table(facts.roster, facts.launched)),
    ]
    if runner_only:
        steps.append(PrintLines(("", _INFRA_SKIPPED)))
    else:
        steps += [PrintLines(("", _INFRA)), ProbeDataPlane()]
    steps += [PrintLines(("", _HOST)), ReadHostResources()]
    if facts.lgtm_host:
        steps += [PrintLines(("", _LGTM)), ProbeLgtm()]
    if not runner_only:
        steps += [PrintLines(("", _REDIS_BRIDGE)), ProbeRedisBridge()]
    if facts.roles:
        steps.append(ReadSourceDrift())
    steps += [PrintLines(("",)), ReadRelease(facts.repo), ReadGateway()]
    return tuple(steps)
