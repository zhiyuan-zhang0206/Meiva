# meiva:note
# What an SDK call is stamped with, decided over facts the recorder read: the identity an SDK
# usage event carries, and the refusals of a remote admission that cannot carry a borrowed
# lease. Nothing here reads or writes the process.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from .identity_plan import CallerFacts


def lease_refusal(identity: CallerFacts | None) -> str | None:
    if identity is not None and identity.has_lease:
        return "strong task assignment does not support a borrowed lease"
    return None


def actor_id_refusal(actor: object) -> tuple[type[Exception], str] | None:
    if isinstance(actor, bool) or not isinstance(actor, int):
        return TypeError, "actor agent id must be an integer"
    if actor <= 0:
        return ValueError, "actor agent id must be positive"
    return None


def usage_identity(
    borrowed_agent_id: int | None,
    own: CallerFacts | None,
    external_source: str | None,
) -> dict[str, object]:
    agent_id = borrowed_agent_id
    if agent_id is None and own is not None:
        agent_id = own.agent_id
    actor = None if own is None else own.actor
    source = f"agent:{agent_id}" if agent_id else (actor or "system")
    if external_source is not None and borrowed_agent_id is None:
        source = external_source
    return {"agent_id": agent_id, "source": source}
