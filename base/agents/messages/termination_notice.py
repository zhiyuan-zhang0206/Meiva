from __future__ import annotations

from dataclasses import dataclass

from base.agents.messages.kwargs import NoteTag
from base.agents.messages.lifecycle_text import by_who
from base.api_contracts.agent_contract import parse_spawner_agent_id
from base.host.boundary.json_values import is_object_dict

__all__ = [
    "CRASH_HARVEST_SOURCE",
    "TERMINATION_MESSAGE_PAYLOAD_KEY",
    "AncestorTerminationNotice",
    "ancestor_termination_payload",
    "format_ancestor_termination_notice",
    "should_notify_ancestor",
    "terminate_accepted_text",
    "termination_actor_agent_id",
    "termination_message_of",
]

TERMINATION_MESSAGE_PAYLOAD_KEY = "termination_message"
CRASH_HARVEST_SOURCE = "corpse_reaper"


@dataclass(frozen=True, slots=True)
class AncestorTerminationNotice:
    ancestor_id: int
    inbound_id: int


def termination_actor_agent_id(source: str, *, terminated_agent_id: int) -> int | None:
    if source == "self":
        return terminated_agent_id
    return parse_spawner_agent_id(source)


def should_notify_ancestor(
    nearest_living_ancestor_id: int | None,
    *,
    source: str,
    terminated_agent_id: int,
) -> bool:
    if nearest_living_ancestor_id is None:
        return False
    actor_id = termination_actor_agent_id(source, terminated_agent_id=terminated_agent_id)
    return actor_id != nearest_living_ancestor_id


def termination_message_of(content: str | None, payload: object) -> str | None:
    cleaned = content.strip() if content else ""
    if cleaned:
        return cleaned
    if not is_object_dict(payload):
        return None
    raw = payload.get(TERMINATION_MESSAGE_PAYLOAD_KEY)
    if not isinstance(raw, str):
        return None
    payload_cleaned = raw.strip()
    return payload_cleaned or None


def _format_initiator(source: str) -> str:
    if source == "self":
        return "self"
    actor_id = parse_spawner_agent_id(source)
    if actor_id is not None:
        return f"agent #{actor_id}"
    return source


def format_ancestor_termination_notice(
    *,
    terminated_agent_id: int,
    label: str | None,
    source: str,
    message: str | None,
) -> str:
    cleaned_label = label.strip() if label else ""
    subject = (
        f"Agent #{terminated_agent_id} ({cleaned_label})"
        if cleaned_label
        else f"Agent #{terminated_agent_id}"
    )
    initiator = _format_initiator(source)
    header = f"{subject} terminated (by {initiator})."
    cleaned_message = message.strip() if message else ""
    if not cleaned_message:
        return header
    return f"{header}\n\nMessage:\n{cleaned_message}"


def ancestor_termination_payload(
    *,
    terminated_agent_id: int,
    source: str,
) -> dict[str, object]:
    return {
        "note_tag": NoteTag.AGENT_TERMINATED.value,
        "terminated_agent_id": terminated_agent_id,
        "termination_source": source,
    }


def terminate_accepted_text(source: str, message: str | None = None) -> str:
    head = f"Termination was accepted from {by_who(source)}"
    cleaned = message.strip() if message else ""
    return f"{head}: {cleaned}" if cleaned else head
