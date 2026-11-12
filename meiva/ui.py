"""User delivery and questioning namespace — publish updates and ask questions."""

from __future__ import annotations

__all_for_meiva__ = [
    "ask",
    "close_question",
    "publish",
]

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
    context = process_context.current_context()
    result = transport.post(
        f"/api/agents/{agent_identity.require_agent_id(context)}/user-messages",
        body.model_dump(mode="json"),
        idempotency_key=context.sdk_operation_uid(),
        context=context,
    )
    result.raise_for_status()
    return UserMessage.model_validate(transport.json_object(result)).id


def ask(questions: list[dict[str, object]]) -> list[int]:
    """Ask the user one or more persistent questions without ending your turn.

    questions is a non-empty list; each item has title, optional content, optional
    type (text by default, single_select or multi_select), options for the choice
    types (ordered, each with id, label and optional description) and an optional
    note_suggestion. Text questions have one answer field; choices keep an optional
    Note, and note_suggestion offers a candidate answer or Note that the user must
    explicitly apply before submitting. Any item that is invalid rejects the whole
    call. Returns one question id per item, in the order given.
    The user answers all of your open questions in one reply (each answered or
    skipped), which arrives as one message with a "Re: question #<id>" block per
    question. Questions do not expire; close_question closes one you no longer
    need, and a question cannot be edited. The user is notified once per agent.
    The runtime owns retry identity.
    """
    from base.agents.context.gateway_client import transport
    from base.api_contracts.ask import AskBatch, AskResult

    if not isinstance(questions, list) or not questions:
        raise ValueError("questions must be a non-empty list of dicts")
    allowed = {"title", "content", "type", "options", "note_suggestion"}
    items: list[dict[str, object]] = []
    for index, raw in enumerate(questions):
        if not isinstance(raw, dict):
            raise TypeError(f"questions[{index}] must be a dict")
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"questions[{index}] has unknown keys: {sorted(unknown)}")
        spec: dict[str, object] = {"type": raw.get("type", "text")}
        if "options" in raw:
            spec["options"] = raw["options"]
        if "note_suggestion" in raw:
            spec["note_suggestion"] = raw["note_suggestion"]
        items.append({"title": raw.get("title"), "content": raw.get("content"), "question": spec})
    body = AskBatch.model_validate({"questions": items})
    context = process_context.current_context()
    result = transport.post(
        f"/api/agents/{agent_identity.require_agent_id(context)}/asks",
        body.model_dump(mode="json"),
        idempotency_key=context.sdk_operation_uid(),
        context=context,
    )
    result.raise_for_status()
    return AskResult.model_validate(transport.json_object(result)).ids


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
    context = process_context.current_context()
    result = transport.post(
        f"/api/agents/{agent_identity.require_agent_id(context)}/asks/{question_id}/close",
        {},
        idempotency_key=context.sdk_operation_uid(),
        context=context,
    )
    if result.status_code == 404:
        raise LookupError(f"question {question_id} does not exist")
    if result.status_code == 409:
        raise ValueError(f"question {question_id} is not open: the user already resolved it")
    result.raise_for_status()
