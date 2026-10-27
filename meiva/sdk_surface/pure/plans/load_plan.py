# meiva:note
# What a plugin-load request does, decided from facts its caller read. The caller reads the
# installation slot, the bound context and the launch environment once and builds the facts;
# these functions only choose the step, so the order of the checks (a recorded failure first,
# then an installation that already satisfies the request, then a load in flight) is tested
# as a table instead of through a process. Nothing here reads or writes the process.
# meiva:link meiva/docs/boot-contracts.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class LoadStep(Enum):
    RAISE_FAILURE = auto()
    BIND_CATALOG = auto()
    NOTHING = auto()
    LOAD_SURFACE = auto()
    UPGRADE_FACES = auto()


@dataclass(frozen=True, slots=True)
class IdentityFacts:
    has_agent_id: bool
    owns_loop: bool


@dataclass(frozen=True, slots=True)
class LaunchFacts:
    host: bool
    context_bound: bool
    identity: IdentityFacts | None
    env_has_agent_id: bool


@dataclass(frozen=True, slots=True)
class LoadFacts:
    surface: bool
    installed: bool
    faces: bool
    attempted: bool
    failed: bool
    config_supplied: bool
    context_bound: bool
    launched: bool


@dataclass(frozen=True, slots=True)
class LoadPlan:
    step: LoadStep
    prepare_launched: bool = False


def launched_process(facts: LaunchFacts) -> bool:
    if facts.host:
        return False
    if facts.context_bound:
        identity = facts.identity
        return identity is not None and identity.has_agent_id and not identity.owns_loop
    return facts.env_has_agent_id


def plan_load(facts: LoadFacts) -> LoadPlan:
    if facts.failed:
        return LoadPlan(LoadStep.RAISE_FAILURE)
    if facts.installed and (facts.surface or facts.faces):
        return LoadPlan(LoadStep.BIND_CATALOG)
    if facts.attempted:
        return LoadPlan(LoadStep.NOTHING)
    if facts.installed:
        return LoadPlan(LoadStep.UPGRADE_FACES)
    own_launch = not facts.config_supplied and not facts.context_bound and facts.launched
    return LoadPlan(LoadStep.LOAD_SURFACE, prepare_launched=own_launch)


class MissingNameStep(Enum):
    IGNORE = auto()
    RAISE_FAILURE = auto()
    ASK_LAUNCHED = auto()
    LOADED = auto()
    LOAD = auto()


@dataclass(frozen=True, slots=True)
class MissingNameFacts:
    private: bool
    entry_complete: bool
    failed: bool
    installed: bool
    attempted: bool
    launched_child: bool | None


def plan_missing_name(facts: MissingNameFacts) -> MissingNameStep:
    if facts.private or not facts.entry_complete:
        return MissingNameStep.IGNORE
    if facts.failed:
        return MissingNameStep.RAISE_FAILURE
    if facts.launched_child is None:
        if facts.installed or facts.attempted:
            return MissingNameStep.IGNORE
        return MissingNameStep.ASK_LAUNCHED
    if facts.installed:
        return MissingNameStep.LOADED
    if not facts.launched_child:
        return MissingNameStep.IGNORE
    return MissingNameStep.LOAD
