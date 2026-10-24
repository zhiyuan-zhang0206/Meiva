"""What `meiva.agents` checks and does around a peer call, apart from the gateway round trip.

The namespace module `meiva/agents/__init__.py` is the facade: it validates arguments, reads the
caller's identity and talks to the gateway client. The steps that only need values live here
as functions of explicit arguments, so a test calls them directly with the inputs and the scan
it wants instead of replacing the gateway client or the injection scan.
"""

from __future__ import annotations

from dataclasses import dataclass

from base.agents.messages.kwargs import NoteTag
from base.api_contracts.idempotency import validate_idempotency_key
from meiva.sdk_surface.validation import coerce_str, coerce_typed

__all__ = ["MessageScan", "SystemNote", "last_message_scan", "validate_system_note"]


@dataclass(frozen=True)
class SystemNote:
    """A validated `send_system_note` request, ready to hand to the gateway."""

    agent_id: int
    content: str
    tag: str
    task_id: int | None
    resurrect: bool
    idempotency_key: str


def validate_system_note(
    agent_id: int,
    content: str,
    *,
    tag: str,
    task_id: int | None,
    resurrect: bool,
    idempotency_key: str,
) -> SystemNote:
    """Check one `send_system_note` request before anything reaches the gateway.

    Raises:
        ValueError: `tag` is not a NoteTag value, `task_id` is not positive, or a `task_id`
            comes with a tag other than `task`.
    """
    agent_id = coerce_typed(agent_id, "agent_id", int)
    content = coerce_str(content, "content")
    tag = coerce_str(tag, "tag")
    task_id = coerce_typed(task_id, "task_id", int, allow_none=True)
    if task_id is not None and task_id <= 0:
        raise ValueError(f"task_id must be a positive integer, got {task_id!r}")
    try:
        NoteTag(tag)
    except ValueError as exc:
        valid_tags = ", ".join(member.value for member in NoteTag)
        raise ValueError(f"tag must be one of: {valid_tags}; got {tag!r}") from exc
    if task_id is not None and tag != NoteTag.TASK.value:
        raise ValueError("task_id requires tag='task'")
    resurrect = coerce_typed(resurrect, "resurrect", bool)
    return SystemNote(
        agent_id=agent_id,
        content=content,
        tag=tag,
        task_id=task_id,
        resurrect=resurrect,
        idempotency_key=validate_idempotency_key(idempotency_key),
    )


# meiva:note
# What to hand the injection scan for a peer's latest turn text.
@dataclass(frozen=True)
class MessageScan:
    text: str
    source: str


# meiva:note
# The scan a peer's latest turn text is owed, or None when there is no text. The facade runs it
# through `meiva.security.scan_content`, which records a finding for suspicious text and leaves the
# content unchanged. A missing message is not scanned.
def last_message_scan(message: str | None, agent_id: int) -> MessageScan | None:
    if message is None:
        return None
    return MessageScan(text=message, source=f"peer.last_message:{agent_id}")
