"""meiva_fleet plugin — an agent's whole surface to a human supervising the fleet.

Everything an agent shows a human is registered here, and all of it presumes a
human is watching, so disabling the plugin (declaring an unsupervised, fully
autonomous deployment) strips the whole surface at once. Two groups:

**Self-presentation** (`meiva.self.*`, pull — the human scans the monitoring view):

1. **Label** `meiva.self.set_label(text)` — the agent's role / name shown next to
   it, and stated in the agent's own agent-ID context note at each window
   establishment. A label the agent sets itself sticks (it is not replaced
   automatically afterwards).

**Notices to the user's queue** (`meiva.ui.*`, push — the agent grabs triage):

Notices land in an aggregated, asynchronous queue the user checks later — they
are for when the user is not in a live conversation with the agent. In a live
dialog the user reads your replies as you write them, so answer directly and
do not post a notice.

2. **Post** `meiva.ui.notify(title, content, idempotency_key=notice_key,
   blocking=..., priority=...)` — request one answer from the user.
   Ordinary information belongs in chat. Priority is P0 through P3;
   `blocking=True` means progress depends on the answer.
3. **Edit** `meiva.ui.edit_notice(...)` — revise the notice you posted that the
   user has not acted on yet (at most one is open, so no id is needed).
4. **Dismiss** `meiva.ui.dismiss_notice()` — withdraw the open notice that is no
   longer relevant, so the user does not spend time on a stale entry.

Each call publishes an update so the view live-refreshes. The plugin also bundles
the `meiva_fleet` skill (the orchestration discipline). Disabled -> members absent
(`meiva.ui` falls back to pages-only), no prompt, no skill, zero overhead.
"""

__description__: str = (
    "Fleet supervision surface — the agent self-reports its role/label and posts "
    "questions to the user's queue, all requiring a response, which it can edit "
    "or dismiss; the monitoring view shows all of it live."
)

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import meiva
import meiva.sdk_surface.agent_identity
from base.agents.tasks.priority import validate_priority
from base.packages.plugins.extensions import PluginContributions, SdkMember, SdkNamespace
from meiva.sdk_surface.validation import coerce_str, coerce_typed

from . import task_registry


def set_label(text: str) -> None:
    text = coerce_str(text, "text", allow_none=True)
    agent_id = meiva.sdk_surface.agent_identity.require_agent_id()
    from meiva import gateway_client

    gateway_client.patch_label(agent_id, text or "", source="self")


# Sentinel for edit_notice: distinguishes "argument not passed" from an explicit
# None (clear the field). Using None for both would silently turn "leave content
# alone" into "erase content". Carries a repr because it renders as the default
# in the SDK stub the agent reads — a bare object() would print its memory
# address there.


class _Unset:
    def __repr__(self) -> str:
        return "<unchanged>"


_UNSET: object = _Unset()


# ── notice row shape (one `agent_notices` row, the pending summary) ──────────
# The lightweight summary notify() returns; a TypedDict, so an agent still
# reads it as a plain dict.


class PendingNoticeRow(TypedDict):
    """The lightweight summary row for one open notice (notify's pending list)."""

    id: int
    title: str
    created_at: datetime
    priority: str


class Notice(int):
    """The notice's id (an int, usable wherever a notice id is expected).
    `.pending_notices` / `.pending_count` are your still-open notices."""

    pending_count: int
    pending_notices: list[PendingNoticeRow]

    def __new__(
        cls,
        id: int,
        *,
        pending_count: int,
        pending_notices: list[PendingNoticeRow],
    ) -> "Notice":
        instance = super().__new__(cls, id)
        instance.pending_count = pending_count
        instance.pending_notices = pending_notices
        return instance


def _raise_as_value_error(resp: Any) -> None:
    """Raise gateway 409/422 refusals as ValueError — the SDK's validation
    contract (fail fast with a clear message the agent can act on). Any other
    error propagates through the normal wire contract."""
    from base.agents.context.gateway_client import transport

    try:
        transport.raise_from_response(resp)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status not in (409, 422):
            raise
        detail = e.response.json().get("detail")  # type: ignore[attr-defined]
        raise ValueError(detail if isinstance(detail, str) else "invalid request") from e


def notify(
    title: str,
    content: str | None = None,
    *,
    require_response: bool = True,
    blocking: bool = False,
    priority: str = "P2",
    task: int | None = None,
    expire_at: datetime | timedelta | str | None = None,
    idempotency_key: str,
    question: dict[str, object] | None = None,
) -> "Notice":
    """Post one notice to the user's queue.

    Every notice requires a user response. Ordinary information, progress and
    results belong in chat. `require_response=False` is rejected.
    `priority` is P0 (highest) through P3 (lowest), independent of `blocking`.
    `blocking=True` means you cannot make progress until the user answers.

    Posting returns immediately. A user reply arrives later as an ordinary
    message; continue work you can do while waiting. You hold one open notice at
    a time: calling notify() again while one is open raises, naming that notice's
    id, and nothing is replaced behind your back. Close the open one with
    dismiss_notice, or rewrite it in place with edit_notice, then post the new one.

    Args:
        content: optional background for the question in title.
        question: one structured question: type=text, or type=single_select /
            multi_select with ordered options (id, label, optional description)
            with an optional comment alongside the selection; the user may also
            answer with a comment alone, choosing no option at all. No Other option.
            Options are numbered from 1. The question and its context are immutable.
        task: groups your notices by task in the user's queue.
        expire_at: expiration deadline; omit it to use the configured lifetime.
        idempotency_key: reuse for the same notice across explicit retries.
            Use an absolute expire_at when retrying with a key.

    Returns:
        The notice id (an int), carrying `.pending_notices` and `.pending_count`.
    """
    title = coerce_str(title, "title")
    content = coerce_str(content, "content", allow_none=True)
    require_response = coerce_typed(require_response, "require_response", bool)
    blocking = coerce_typed(blocking, "blocking", bool)
    priority = coerce_str(priority, "priority")
    task = coerce_typed(task, "task", int, allow_none=True)
    if not title.strip():
        raise ValueError("title must be non-empty")
    validate_priority(priority)
    if not require_response:
        raise ValueError("notices require a user response; send ordinary information in chat")

    from base.api_contracts.notice_question import ChoiceQuestion, parse_question

    parsed_question = parse_question(question) if question is not None else None
    if isinstance(parsed_question, ChoiceQuestion) and parsed_question.allow_other:
        raise ValueError("Other is not an answer option; use a choice with an optional comment")

    expire_at_iso: str | None = None
    if expire_at is not None:
        from base.daemon.schedules.watcher import normalize_when

        due_at = normalize_when(expire_at)
        if due_at < datetime.now(UTC):
            raise ValueError(
                f"expire_at is in the past: {due_at.isoformat()}. "
                "Provide a future time, or use a positive timedelta."
            )
        expire_at_iso = due_at.isoformat()

    from base.api_contracts.idempotency import validate_idempotency_key

    idempotency_key = validate_idempotency_key(idempotency_key)
    aid = meiva.sdk_surface.agent_identity.require_agent_id()

    from base.agents.context.gateway_client import transport

    resp = transport.post(
        f"/api/agents/{aid}/notices",
        {
            "title": title,
            "content": content,
            "priority": priority,
            "require_response": require_response,
            "blocking": blocking,
            "task_id": task,
            "expire_at": expire_at_iso,
            **(
                {"question": parsed_question.model_dump(mode="json")}
                if parsed_question is not None
                else {}
            ),
        },
        idempotency_key=idempotency_key,
        context=meiva.context,
    )
    _raise_as_value_error(resp)
    data = resp.json()
    pending_rows: list[PendingNoticeRow] = [
        PendingNoticeRow(
            id=int(r["id"]),
            title=r["title"],
            created_at=datetime.fromisoformat(r["created_at"]),
            priority=r["priority"],
        )
        for r in data["pending_notices"]
    ]
    return Notice(
        int(data["id"]),
        pending_count=int(data["pending_count"]),
        pending_notices=pending_rows,
    )


def edit_notice(
    *,
    title: str = _UNSET,  # type: ignore[assignment]
    content: str | None = _UNSET,  # type: ignore[assignment]
    priority: str = _UNSET,  # type: ignore[assignment]
    blocking: bool = _UNSET,  # type: ignore[assignment]
) -> None:
    """Edit the open response-required notice without changing its question.

    Args:
        content: pass None to clear.
        blocking: only valid on a notice that needs a response.
    """
    if title is not _UNSET:
        title = coerce_str(title, "title")
    if content is not _UNSET:
        content = coerce_str(content, "content", allow_none=True)
    if priority is not _UNSET:
        priority = coerce_str(priority, "priority")
    if blocking is not _UNSET:
        blocking = coerce_typed(blocking, "blocking", bool)
    if title is not _UNSET and not title.strip():
        raise ValueError("title must be non-empty")
    if priority is not _UNSET:
        validate_priority(priority)

    body: dict[str, object] = {}
    if title is not _UNSET:
        body["title"] = title
    if content is not _UNSET:
        body["content"] = content
    if priority is not _UNSET:
        body["priority"] = priority
    if blocking is not _UNSET:
        body["blocking"] = blocking
    if not body:
        raise ValueError("edit_notice needs at least one field to change")

    aid = meiva.sdk_surface.agent_identity.require_agent_id()
    observed = _observe_open_notice_id(aid)
    if observed is None:
        return

    # One unified write path (R3 door ④): the gateway edits exactly the observed
    # notice row and re-publishes the posted event.
    from base.agents.context.gateway_client import transport

    resp = transport.patch(
        f"/api/agents/{aid}/notices/current/guarded-v1",
        {"observed_notice_id": observed, **body},
        idempotency_key=uuid.uuid4().hex,
        context=meiva.context,
    )
    _raise_as_value_error(resp)


def dismiss_notice() -> None:
    """Withdraw the open notice. One notice is open per agent and this call
    observes it, so no id is needed; with none open it does nothing."""
    aid = meiva.sdk_surface.agent_identity.require_agent_id()
    observed = _observe_open_notice_id(aid)
    if observed is None:
        return
    # One unified write path (R3 door ④): the gateway withdraws exactly the
    # observed notice row and publishes the resolve + agent-updated events.
    from base.agents.context.gateway_client import transport

    resp = transport.post(
        f"/api/agents/{aid}/notices/current/dismiss/guarded-v1",
        {"observed_notice_id": observed},
        idempotency_key=uuid.uuid4().hex,
        context=meiva.context,
    )
    _raise_as_value_error(resp)


def _observe_open_notice_id(agent_id: int) -> int | None:
    """Global id of the agent's open notice, or None when it has none.

    The guarded edit/withdraw routes target an observed global row, so a retry
    of an ambiguous call can never touch a notice posted later.
    """
    from base.agents.context.gateway_client import transport

    resp = transport.get(
        "/api/notices/open",
        params={"agent_id": agent_id, "include_awaiting": True},
        context=meiva.context,
    )
    transport.raise_from_response(resp)
    rows = resp.json()
    return int(rows[0]["id"]) if rows else None


# ── meiva.tasks SDK namespace — the task registry (see task_registry.py) ───────
# A whole-module namespace (like meiva_code's meiva.cwd), not members hung on an
# existing group: the registry is a cohesive new surface, and the core top level
# stays small. `expand=True` promotes it into the in-prompt SDK reference
# so agents discover meiva.tasks.* without drilling in.


def contribute() -> PluginContributions:
    """What this plugin declares for the SDK surface."""
    return PluginContributions(
        sdk_namespaces=(SdkNamespace("tasks", task_registry, expand=True),),
        sdk_members=(
            SdkMember("ui", "notify", notify),
            SdkMember("ui", "edit_notice", edit_notice),
            SdkMember("ui", "dismiss_notice", dismiss_notice),
            SdkMember("self", "set_label", set_label),
        ),
    )
