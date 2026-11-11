"""User delivery and questioning namespace — publish updates and ask questions."""

from __future__ import annotations

__all_for_meiva__ = [
    "ask",
    "close_question",
    "publish",
]

from base.host.boundary.fields import int_field
from meiva.sdk_surface import agent_identity, process_context


def publish(content: str | None, *, suggestions: list[str] | None = None) -> int:
    """Publish information without ending your turn (SDK delivery mode).

    Optional suggestions are Agent-authored candidates for the independent composer
    service. Use ask for a question that needs an answer. Reading does not imply approval.
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
    """Ask a persistent question without ending your turn.

    question accepts type=text (default), single_select or multi_select with ordered
    options (id, label, optional description). Text questions have one answer field;
    choices keep an optional Note. note_suggestion offers a candidate answer or Note
    that the user must explicitly apply before submitting.
    Each call asks one question and returns its own global question id; several may
    remain open. Questions do not expire or replace earlier ones. The user's answer
    arrives asynchronously as a message. close_question closes one you no longer need; a question cannot be edited. The user is notified
    of each question. The runtime owns retry identity. Returns the global question id.
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


def close_question(question_id: int) -> None:
    """Close one of your own unanswered questions; the user is no longer asked it.

    Takes an id that ask returned. The question's notification is withdrawn and
    nothing is delivered to you. Closing a question that is already closed returns
    quietly. A question that does not exist or is not yours raises LookupError. A
    question the user already answered or dismissed raises ValueError: it is not
    open to close. The runtime owns retry identity.
    """
    from base.agents.context.gateway_client import transport

    if isinstance(question_id, bool) or not isinstance(question_id, int):
        raise TypeError("question_id must be an int")
    context = process_context.require_bound_context()
    result = transport.post(
        f"/api/agents/{agent_identity.require_agent_id()}/asks/{question_id}/close",
        {},
        idempotency_key=context.sdk_operation_uid(),
        context=context,
    )
    if result.status_code == 404:
        raise LookupError(f"question {question_id} does not exist")
    if result.status_code == 409:
        raise ValueError(f"question {question_id} is not open: the user already resolved it")
    result.raise_for_status()
