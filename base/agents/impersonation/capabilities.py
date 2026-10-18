# meiva:note
# A lease may declare which SDK surfaces its controller may use. The declaration
# is stored on the lease row (`agent_impersonations.capabilities`) as a JSON array
# of Capability values, with `capability_declared_by` naming how it arose.
#
# Three stored states are distinct and must never be conflated: an array grants
# exactly those surfaces (possibly none); NULL is a lease issued before this
# vocabulary and keeps the full surface it was issued with - **never read NULL as
# the minimal set**; a non-array value is refused by the column's CHECK.
#
# The vocabulary is closed and concrete: it names SDK surfaces, not abstract
# authority. An SDK entry with no capability belongs nowhere, and the SDK-side map
# (`meiva/impersonation/capabilities.py`) plus its coverage test keep that true for
# every agent-visible entry. Connection handles (`meiva.context.sql` / `.redis` /
# `.gateway`) are not SDK entries, so no capability names them: this layer cannot
# intercept them, and naming them would describe an enforcement that does not exist.
# meiva:link base/agents/impersonation/docs/capabilities.meiva.okf.md

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any, cast


class Capability(StrEnum):
    READ = "read"
    FILES_WRITE = "files.write"
    SHELL = "shell"
    AGENTS_SPAWN = "agents.spawn"
    AGENTS_CONTROL = "agents.control"
    AGENTS_MESSAGE = "agents.message"
    SELF_LIFECYCLE = "self.lifecycle"
    TASKS_WRITE = "tasks.write"
    UI = "ui"
    SCHEDULE = "schedule"


# meiva:note
# A profile is a named bundle of capabilities; the lease stores the expanded set,
# so a profile may change without rewriting issued leases. `minimal` is what a
# takeover needs to read, report and answer. `machine` is a coding takeover
# working on this machine; `shell` there is machine authority in practice - it
# reaches every file and credential the platform user can read - and the set
# deliberately stops short of the platform surfaces (control, task registry, self
# lifecycle, scheduling, user UI), which only an explicit declaration grants.
MINIMAL_PROFILE = frozenset({Capability.READ, Capability.AGENTS_MESSAGE})
MACHINE_PROFILE = frozenset(
    {
        Capability.READ,
        Capability.AGENTS_MESSAGE,
        Capability.FILES_WRITE,
        Capability.SHELL,
    }
)
PROFILES: Mapping[str, frozenset[Capability]] = {
    "minimal": MINIMAL_PROFILE,
    "machine": MACHINE_PROFILE,
}

# meiva:note
# How a declaration arose. `legacy` marks a pre-vocabulary lease whose capabilities
# stay NULL; `launcher-default` marks an automatic takeover that declared nothing,
# which the compatibility window still expands to `machine`.
DECLARED_EXPLICIT = "explicit"
DECLARED_CALLER_DEFAULT = "caller-default"
DECLARED_LAUNCHER_DEFAULT = "launcher-default"
DECLARED_LEGACY = "legacy"
DECLARATIONS = frozenset(
    {DECLARED_EXPLICIT, DECLARED_CALLER_DEFAULT, DECLARED_LAUNCHER_DEFAULT, DECLARED_LEGACY}
)


class CapabilityError(RuntimeError):
    pass


# meiva:note
# `CapabilityDenied` names the outcome the caller handles, not an error kind, so it
# keeps the N818 exemption the repository uses for outcome-style names.
class CapabilityDenied(CapabilityError):  # noqa: N818
    def __init__(self, entry: str, capability: Capability | None, mode: str) -> None:
        self.entry = entry
        self.capability = capability
        self.mode = mode
        needed = (
            "no declared capability" if capability is None else f"capability {capability.value!r}"
        )
        super().__init__(
            f"meiva.{entry} needs {needed}; this impersonation lease does not grant it "
            f"(enforcement={mode}). Ask the Meiva side for a takeover that declares it."
        )


# meiva:note
# Canonical (sorted, unique) values, or ValueError for an unknown name: the writer
# fails fast, because a typo must not silently grant nothing (or, worse, be read
# loosely later).
def parse_capabilities(values: Iterable[str]) -> tuple[str, ...]:
    parsed = {Capability(value) for value in values}
    return tuple(sorted(capability.value for capability in parsed))


# meiva:note
# `profile_capabilities` expands a named profile; an unknown name raises instead of
# falling back, because a typo must not quietly grant the minimal set to a takeover
# that asked for machine authority.
def profile_capabilities(profile: str) -> tuple[str, ...]:
    try:
        members = PROFILES[profile]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(
            f"unknown capability profile {profile!r}; known profiles: {known}"
        ) from None
    return tuple(sorted(capability.value for capability in members))


# meiva:note
# `declared_capabilities` returns None for a pre-vocabulary lease (legacy-full).
# A stored value this build does not know is not granted: a lease issued by a newer
# build and read by an older one narrows, never widens. A malformed (non-array)
# declaration raises rather than guessing, because the writer always stores an array;
# a JSON array decodes to a Python list, so any other type is that malformed row.
def declared_capabilities(row: Mapping[str, Any]) -> frozenset[Capability] | None:
    raw = row.get("capabilities")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise CapabilityError(
            f"lease {row.get('id')!r} stores a non-array capability declaration: {type(raw).__name__}"
        )
    granted: set[Capability] = set()
    for value in cast("list[str]", raw):
        try:
            granted.add(Capability(value))
        except ValueError:
            continue
    return frozenset(granted)


def capability_granted(row: Mapping[str, Any], capability: Capability) -> bool:
    declared = declared_capabilities(row)
    return declared is None or capability in declared
