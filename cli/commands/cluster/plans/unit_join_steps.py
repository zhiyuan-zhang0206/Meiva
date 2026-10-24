# meiva:note
# The steps of a remote unit's join and the inputs of each, decided from values.
#
# A join authenticates with one credential, fetches what the gateway serves, installs the
# carried capability when there is one, and forgets what the gateway served. Which credential
# a join presents, what the install is handed, what is said once it has landed and which
# served keys the identity values must not keep are decided here; opening the bundle, dialling
# the gateway, installing and deleting the bundle file are the shell's in
# `cli.commands.cluster.unit_join`.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from cli.commands.cluster.plans.unit_join_checks import served_value

__all__ = [
    "CarriedBundle",
    "InstallBundle",
    "InstalledCapability",
    "JoinCredential",
    "installed_line",
    "plan_credential",
    "plan_install",
]


# meiva:note
# The join carries a sealed bundle: it is opened first and its machine API token authenticates
# the fetch. `path` is the bundle file, which the install consumes.
@dataclass(frozen=True)
class CarriedBundle:
    path: str


# meiva:note
# The join carries nothing: the capability a past join installed authenticates the fetch.
@dataclass(frozen=True)
class InstalledCapability:
    pass


JoinCredential = CarriedBundle | InstalledCapability


# meiva:note
# Which credential the join presents. A carried bundle needs its transport key before
# anything is opened; `env_name` names the variable the operator supplies it in, and only
# whether the key was supplied reaches the plan. A join without a bundle never needs the key.
def plan_credential(
    capability_path: str | None, *, key_supplied: bool, env_name: str
) -> JoinCredential:
    if capability_path is None:
        return InstalledCapability()
    if not key_supplied:
        raise ValueError(f"a capability bundle requires its transport key in {env_name}")
    return CarriedBundle(capability_path)


# meiva:note
# What installing a carried bundle is handed: the machine this unit calls itself, the database
# endpoint the gateway serves right now (the bundle must name it), and the bundle file to
# delete once it is installed.
@dataclass(frozen=True)
class InstallBundle:
    machine: str
    served_endpoint: str
    consume_path: str


# meiva:note
# The install a carried bundle is owed after the gateway answered. A bundle is bound to a
# machine name, so a unit whose identity values name none cannot install one.
def plan_install(
    carried: CarriedBundle, served: Mapping[str, str], identity: Mapping[str, str]
) -> InstallBundle:
    machine = identity.get("MEIVA_MACHINE_NAME", "")
    if not machine:
        raise ValueError("a capability bundle is bound to a machine name and this unit has none")
    return InstallBundle(
        machine=machine,
        served_endpoint=served_value(served, "MEIVA_DB_URL"),
        consume_path=carried.path,
    )


# meiva:note
# What the join says once the bundle is installed and its file consumed.
def installed_line(generation: int, bundle_path: str) -> str:
    return (
        f"  ✓ database capability installed: write generation {generation}; "
        f"bundle {bundle_path} consumed"
    )
