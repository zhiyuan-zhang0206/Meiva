"""User delivery and questioning namespace — publish updates and ask questions."""

from __future__ import annotations

__all_for_meiva__ = [
    "ask",
    "publications",
    "publish",
    "questions",
]

from base.api_contracts.user_delivery import UserMessage
from base.host.boundary.fields import int_field
from base.host.boundary.untyped import object_of
from meiva import boot
from meiva.sdk_surface import agent_identity, process_context
from meiva.sdk_surface.validation import coerce_typed


def __getattr__(name: str) -> object:
    # meiva:note
    # Plugin members can extend meiva.ui via a declared `SdkMember`. In an agent-launched
    # persistent-shell child they are absent until plugins load, and this module
    # already exists so meiva.__getattr__ never fires — trigger the shared lazy
    # load here, then retry.
    import sys as _sys

    if boot.maybe_load_plugins_for_missing(name):
        return object_of(getattr, _sys.modules[__name__], name)
    raise AttributeError(f"module 'meiva.ui' has no attribute {name!r}")


def publish(content: str | None, *, suggestions: list[str] | None = None) -> int:
    """Publish information without ending your turn (SDK delivery mode).

    Optional suggestions are Agent-authored candidates for the independent composer
    service. Use ask for a question that needs an answer. Messages remain available
    after overview edits and compaction. Reading does not imply approval.
    Pass content=None to explicitly record no update. The runtime owns retry
    identity. Returns the durable publication id.
    """
    from base.agents.context.gateway_client import transport
    from base.api_contracts.user_delivery import PublishMessage, UserMessage

    body = PublishMessage(content=content, suggestions=[] if suggestions is None else suggestions)
    context = process_context.require_bound_context()
    result = transport.post(
        f"/api/agents/{agent_identity.require_agent_id()}/user-messages",
        body.model_dump(mode="json"),
        idempotency_key=context.sdk_operation_uid(),
        context=context,
    )
    result.raise_for_status()
    return UserMessage.model_validate(transport.json_object(result)).id


def ask(
    title: str,
    content: str | None = None,
    *,
    question: dict[str, object] | None = None,
    note_suggestion: str | None = None,
) -> int:
    """Ask a persistent question without ending your turn or sending a notification.

    question accepts type=text (default), single_select or multi_select with ordered
    options (id, label, optional description). Text questions have one answer field;
    choices keep an optional Note. note_suggestion offers a candidate answer or Note
    that the user must explicitly apply before submitting.
    Multiple questions may remain open. Questions do not expire or replace earlier
    ones. A user answer or explicit close arrives asynchronously. The runtime owns
    retry identity. Returns the global question id.
    """
    from base.agents.context.gateway_client import transport
    from base.api_contracts.user_delivery import AskMessage

    payload: dict[str, object] = {"type": "text"} if question is None else dict(question)
    if note_suggestion is not None:
        payload["note_suggestion"] = note_suggestion
    body = AskMessage.model_validate({"title": title, "content": content, "question": payload})
    context = process_context.require_bound_context()
    result = transport.post(
        f"/api/agents/{agent_identity.require_agent_id()}/asks",
        body.model_dump(mode="json"),
        idempotency_key=context.sdk_operation_uid(),
        context=context,
    )
    result.raise_for_status()
    return int_field(transport.json_object(result), "id")


def publications(
    *, pending_only: bool = True, after_id: int = 0, limit: int = 50
) -> list[UserMessage]:
    """Read your durable user messages without marking them read on the user's behalf.

    Retrieve pending context after compaction or after rewriting to_user.md.
    Returns at most limit messages (1-100), in increasing id order. Continue with
    after_id set to the last returned id. pending_only includes unread messages
    and unanswered required replies; False also returns resolved history.
    """
    from pydantic import TypeAdapter

    from base.agents.context.gateway_client import transport
    from base.api_contracts.user_delivery import UserMessage

    coerce_typed(pending_only, "pending_only", bool)
    coerce_typed(after_id, "after_id", int)
    coerce_typed(limit, "limit", int)
    if after_id < 0 or not 1 <= limit <= 100:
        raise ValueError("after_id must be nonnegative and limit must be between 1 and 100")
    context = process_context.require_bound_context()
    result = transport.get(
        f"/api/agents/{agent_identity.require_agent_id()}/user-messages",
        params={"pending": str(pending_only).lower(), "after_id": after_id, "limit": limit},
        context=context,
    )
    result.raise_for_status()
    return TypeAdapter(list[UserMessage]).validate_python(transport.json_object(result)["items"])


def questions() -> list[dict[str, object]]:
    """Retrieve your pending Ask records, including full options and Note suggestions.

    Reading here never acknowledges a question on the user's behalf. Questions
    remain pending until the user answers or explicitly closes them.
    """
    from base.agents.context.gateway_client import transport

    result = transport.get(
        "/api/notices/open",
        params={"agent_id": agent_identity.require_agent_id(), "limit": 500},
        context=process_context.require_bound_context(),
    )
    result.raise_for_status()
    return transport.json_rows(result)
