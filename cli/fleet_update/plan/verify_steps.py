# meiva:note
# What `up` verifies once the hosts are started, and in which order: a smoke test per serving
# agent-runner, a package refresh and the drift checks on every host. One failure never erases
# what the window learned about the others, and a host that never started is reported skipped,
# not smoke-tested.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .roster import RosterRow


@dataclass(frozen=True)
class SmokeStep:
    owner: str
    machine: str
    runs: bool


@dataclass(frozen=True)
class DriftCheck:
    alias: str
    verb: str


# meiva:note
# One smoke per machine the roster says serves an agent-runner, run on the gateway. The host that
# owns the machine must have started in this run: a machine whose host failed to start is skipped
# with that reason.
def smoke_plan(
    rows: tuple[RosterRow, ...], names: Mapping[str, str], started: frozenset[str]
) -> tuple[SmokeStep, ...]:
    steps: list[SmokeStep] = []
    for row in rows:
        if not row.serve_agent_runner:
            continue
        owner = next(alias for alias, name in names.items() if name == row.name)
        steps.append(SmokeStep(owner, row.name, owner in started))
    return tuple(steps)


# meiva:note
# The drift checks come last and every one runs: the gateway's stored schedules once, the plugins
# on every host.
def drift_checks(gateway: str, hosts: tuple[str, ...]) -> tuple[DriftCheck, ...]:
    return (
        DriftCheck(gateway, "schedules verify --no-notify"),
        *(DriftCheck(alias, "plugins verify") for alias in hosts),
    )


def machine_name(output: str) -> str | None:
    reported = output.split()
    return reported[-1] if reported else None


# meiva:note
# Two listed hosts reporting one machine name would make the roster match ambiguous.
def duplicate_names(names: Mapping[str, str]) -> str | None:
    if len(set(names.values())) != len(names):
        return f"two listed hosts report one machine name: {dict(names)}"
    return None
