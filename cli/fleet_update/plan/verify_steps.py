# meiva:note
# What `up` verifies once the hosts are started, and in which order: the roster gate, a package
# refresh and the drift checks on every host. One failure never erases what the window learned
# about the others.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class DriftCheck:
    alias: str
    verb: str


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
