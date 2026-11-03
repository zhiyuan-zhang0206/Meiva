# meiva:note
# The one write a served page may make.
#
# A page could already post to /api/agents/<id>/messages with the viewer's session, because
# it runs on the gateway origin (task #6058). This route is the exit that lets the page do
# the one thing it legitimately does — answer the question it asked — without handing it the
# whole API surface. It takes a bounded envelope and renders the message itself; everything
# else about the page stays read-only.
#
# Written for the origin the page will move to: the widgets build an absolute URL, send
# `credentials: "include"`, and send JSON (so a cross-origin call preflights). The route
# itself must keep working unchanged when the page is no longer same-origin, which is why it
# reads nothing from `Origin` and requires no header beyond the session cookie.
#
# The reply stays the viewer's action: `require_human_credential` refuses a machine token, so
# the inbound records the same fact it records for a message typed into the UI.

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gateway.agents.inbound_provenance import request_inbound_provenance
from gateway.http.auth.request_principal import require_human_credential
from gateway.http.config import gateway_rpc, gateway_state
from gateway.routers.page_serving.pages import parse_page_key
from ops.agents import get_agent_status
from ops.lifecycle.delivery import deliver_chat_inbound
from ops.pages import get_open_page_target

router = APIRouter()

# meiva:note
# Bounds on the envelope, not on the agent: a reply is a sentence and a few ticks. Text is
# capped well under the 1 MiB transport limit so a page cannot use this as a file channel —
# uploads have their own route and their own limits.
_MAX_TEXT_CHARS = 8_000
_MAX_CHOICES = 50
_MAX_CHOICE_CHARS = 200


# meiva:note
# The bounded envelope a page may submit: which options, and an optional note. `kind` names
# the widget that asked (choice / confirm / compare / form / reply) and is rendered into the
# message, so the agent reads every reply in the same shape. `choices` carries the selected
# option labels as the page showed them; `text` is the free-form part (a confirm note, a
# form's fields, a chat reply).
class PageReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["choice", "confirm", "compare", "form", "reply"] = "reply"
    choices: list[str] = Field(default_factory=list, max_length=_MAX_CHOICES)
    text: str = Field(default="", max_length=_MAX_TEXT_CHARS)

    @field_validator("choices")
    @classmethod
    def _choices_are_short_and_present(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for choice in value:
            stripped = choice.strip()
            if not stripped:
                raise ValueError("a choice must not be blank")
            if len(stripped) > _MAX_CHOICE_CHARS:
                raise ValueError(f"a choice must be at most {_MAX_CHOICE_CHARS} characters")
            cleaned.append(stripped)
        return cleaned


# meiva:note
# `inbound_id` is null only in the delivery shape that writes no message; this route always
# renders text, so it is present in practice but the field follows the delivery contract.
class PageReplyAccepted(BaseModel):
    inbound_id: int | None
    status: str


# meiva:note
# One message shape for every widget: `<kind>: <choices>` on the first line, the free note
# under it when present. A form carries its fields in `text`, so its first line is just
# `form:`.
def page_reply_content(kind: str, choices: list[str], text: str) -> str:
    head = f"{kind}: {', '.join(choices)}" if choices else f"{kind}:"
    note = text.strip()
    return head if not note else f"{head}\n{note}"


# meiva:note
# Whether the page is still open: the registry row is what makes the key mean anything, and
# it is read through the same lookup the proxy dials with.
def page_is_open(pool: ConnectionPool, agent_id: int, name: str) -> bool:
    with pool.connection() as conn:
        return get_open_page_target(conn, agent_id, name) is not None


@router.post(
    "/pages/{page_key}/reply",
    status_code=201,
    dependencies=[Depends(require_human_credential)],
    description=(
        "Submit the answer a served page asked for.\n\n"
        "The body is a bounded envelope (`kind`, `choices`, `text`); the gateway renders it "
        "into one chat message to the page's agent, with `source=ui:page:<name>`. Unknown "
        "fields are rejected (422) and the page must still be open (404).\n\n"
        "Requires the viewer's own credential — a machine token is refused — so the inbound "
        "records the same auth fact as a message typed into the UI."
    ),
)
async def post_page_reply(page_key: str, body: PageReplyIn, request: Request) -> PageReplyAccepted:
    parsed = parse_page_key(page_key)
    if parsed is None:
        raise HTTPException(
            status_code=404, detail=f"invalid page key {page_key!r} (expected <agent_id>-<name>)"
        )
    agent_id, name = parsed

    pool = gateway_state(request).db_pool
    is_open = await asyncio.to_thread(page_is_open, pool, agent_id, name)
    if not is_open:
        raise HTTPException(status_code=404, detail=f"page {name!r} is not open (agent {agent_id})")

    content = page_reply_content(body.kind, body.choices, body.text)
    if content == f"{body.kind}:":
        raise HTTPException(
            status_code=422, detail="a reply carries at least one choice or some text"
        )

    await asyncio.to_thread(get_agent_status, gateway_state(request).db, agent_id)
    delivery = await deliver_chat_inbound(
        pool,
        gateway_state(request).db,
        gateway_state(request).bus,
        agent_id,
        prepare=lambda _conn: content,
        source=f"ui:page:{name}",
        payload=None,
        client_message_id=None,
        provenance=request_inbound_provenance(request),
        local_machine=gateway_state(request).machine.name(),
        rpc=gateway_rpc(request),
    )
    return PageReplyAccepted(inbound_id=delivery.inbound_id, status=str(delivery.status))
