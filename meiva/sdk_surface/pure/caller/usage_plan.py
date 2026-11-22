# meiva:note
# What an SDK call is stamped with, decided over facts the recorder read: the identity an SDK
# usage event carries.
# meiva:link meiva/docs/sdk-surface/sdk-surface.md

from __future__ import annotations

from .identity_plan import CallerFacts


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
