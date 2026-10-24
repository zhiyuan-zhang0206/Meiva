# meiva:note
# Who is acting, decided over facts the caller read: which agent id a call is attributed to,
# which actor string a durable write is stamped with, which refusal an agent-only action earns,
# and the identity an SDK usage event carries. The facade (`agent_identity.py`, the metering
# recorder) reads the bound context, a borrowed lease and the external-caller profile once and
# builds the facts; nothing here reads or writes the process.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


# meiva:note
# The read-only shape of an `AgentIdentity` this module needs; any identity value satisfies it.
class IdentityLike(Protocol):
    @property
    def agent_id(self) -> int | None: ...

    @property
    def actor(self) -> str | None: ...

    @property
    def owns_loop(self) -> bool: ...

    @property
    def lease(self) -> object | None: ...


@dataclass(frozen=True, slots=True)
class CallerFacts:
    agent_id: int | None
    actor: str | None
    owns_loop: bool
    has_lease: bool


@dataclass(frozen=True, slots=True)
class ActorFacts:
    borrowed_agent_id: int | None
    context_supplied: bool
    identity: CallerFacts | None


@dataclass(frozen=True, slots=True)
class ActorDecision:
    actor: str | None
    recheck_load_failure: bool = False


def caller_facts(identity: IdentityLike | None) -> CallerFacts | None:
    if identity is None:
        return None
    return CallerFacts(
        agent_id=identity.agent_id,
        actor=identity.actor,
        owns_loop=bool(identity.owns_loop),
        has_lease=identity.lease is not None,
    )


def attributed_agent_id(borrowed_agent_id: int | None, identity: CallerFacts | None) -> int | None:
    if borrowed_agent_id is not None:
        return borrowed_agent_id
    return None if identity is None else identity.agent_id


def actor_before_external(facts: ActorFacts) -> str | None:
    if facts.borrowed_agent_id is not None:
        return f"agent:{facts.borrowed_agent_id}"
    identity = facts.identity
    if facts.context_supplied and identity is not None:
        if identity.actor is not None:
            return identity.actor
        if identity.agent_id is not None:
            return f"agent:{identity.agent_id}"
    return None


def actor_after_external(facts: ActorFacts, external_source: str | None) -> ActorDecision:
    if external_source is not None:
        return ActorDecision(external_source)
    identity = facts.identity
    if identity is not None and identity.actor is not None:
        return ActorDecision(identity.actor)
    if identity is None or identity.agent_id is None:
        return ActorDecision(
            None, recheck_load_failure=identity is None and not facts.context_supplied
        )
    return ActorDecision(f"agent:{identity.agent_id}")


def self_action_refusal(identity: CallerFacts | None, action: str) -> str | None:
    if identity is not None and not identity.owns_loop:
        return (
            f"meiva.self.{action}() can only be called from inside an agent process, "
            f"not from a background script launched by one"
        )
    if identity is None or identity.agent_id is None:
        return (
            f"meiva.self.{action}() needs an established agent identity; this process "
            f"never ran the agent bootstrap (agent id is unset)"
        )
    return None
