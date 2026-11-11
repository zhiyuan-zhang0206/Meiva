# meiva:note
# `meiva agents get` and `meiva agents inbox` — one agent's state and its inbound queue, read
# through `GET /api/agents/{id}` and `GET /api/agents/{id}/inbound`.
#
# Thin clients: `get` prints the agent's identity, status, machine and activity clocks; `inbox`
# forwards its filters and prints the queue newest first, the content cut to a preview unless
# `--full` asks for the whole text. The window and page size are the gateway's unless named.

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel

from base.cluster.machine import GatewayAccess
from cli.boundary.gateway_read import fetch, print_json, render_table


class _Agent(BaseModel):
    agent_id: int
    label: str | None
    status: str
    harness: str
    machine: str
    liveness_state: str
    spawner: str
    spawned_at: datetime
    last_active_at: datetime
    last_inbound_at: datetime
    heartbeat_paused_until: datetime | None
    harness_exit_code: int | None
    notices_awaiting_response: list[object]


def _stamp(moment: datetime | None) -> str:
    return "" if moment is None else moment.isoformat(timespec="seconds")


# meiva:note
# Print one agent's identity, lifecycle status, machine and activity clocks.
def cmd_agents_get(access: GatewayAccess, agent_id: int, *, as_json: bool) -> int:
    resp = fetch(access, f"/api/agents/{agent_id}", {})
    if resp is None:
        return 1
    if as_json:
        print_json(resp)
        return 0
    agent = _Agent.model_validate_json(resp.content)
    exit_code = "" if agent.harness_exit_code is None else str(agent.harness_exit_code)
    print(f"id:                {agent.agent_id}")
    print(f"label:             {agent.label or ''}")
    print(f"status:            {agent.status}")
    print(f"machine:           {agent.machine} ({agent.liveness_state})")
    print(f"harness:           {agent.harness}")
    print(f"spawned by:        {agent.spawner}")
    print(f"spawned:           {_stamp(agent.spawned_at)}")
    print(f"last active:       {_stamp(agent.last_active_at)}")
    print(f"last inbound:      {_stamp(agent.last_inbound_at)}")
    print(f"heartbeat paused:  {_stamp(agent.heartbeat_paused_until)}")
    print(f"harness exit code: {exit_code}")
    print(f"open questions:    {len(agent.notices_awaiting_response)}")
    return 0


# meiva:note
# The filters `meiva agents inbox` forwards; `None` means the operator did not set it.
@dataclass(frozen=True)
class InboxFilter:
    status: str | None = None
    kind: str | None = None
    source: str | None = None
    hours: float | None = None
    limit: int | None = None
    full: bool = False


class _InboxItem(BaseModel):
    id: int
    kind: str
    source: str | None
    status: str
    created_at: datetime
    claimed_at: datetime | None
    content: str
    content_truncated: bool


class _InboxMeta(BaseModel):
    window_from: datetime
    has_more: bool


class _Inbox(BaseModel):
    meta: _InboxMeta
    items: list[_InboxItem]


# meiva:note
# One line of text: a newline shows as the two characters `\n`, so a row stays a row.
def _one_line(text: str) -> str:
    return text.replace("\r", "").replace("\n", "\\n")


# meiva:note
# Print the agent's inbound queue, newest first: id, created, status, kind, source and the content.
# With `--full` each message is a header line followed by its whole text.
def cmd_agents_inbox(
    access: GatewayAccess, agent_id: int, flt: InboxFilter, *, as_json: bool
) -> int:
    resp = fetch(
        access,
        f"/api/agents/{agent_id}/inbound",
        {
            "status": flt.status,
            "kind": flt.kind,
            "source": flt.source,
            "hours": flt.hours,
            "limit": flt.limit,
            "full": True if flt.full else None,
        },
    )
    if resp is None:
        return 1
    if as_json:
        print_json(resp)
        return 0
    inbox = _Inbox.model_validate_json(resp.content)
    since = _stamp(inbox.meta.window_from)
    if not inbox.items:
        print(f"(no inbound messages since {since})")
        return 0
    if flt.full:
        for item in inbox.items:
            claimed = f" claimed {_stamp(item.claimed_at)}" if item.claimed_at else ""
            print(
                f"--- #{item.id} {_stamp(item.created_at)} {item.status} {item.kind} "
                f"{item.source or '-'}{claimed}"
            )
            print(item.content)
    else:
        print(
            render_table(
                ("id", "created", "status", "kind", "source", "content"),
                [
                    (
                        str(item.id),
                        _stamp(item.created_at),
                        item.status,
                        item.kind,
                        item.source or "",
                        _one_line(item.content) + ("..." if item.content_truncated else ""),
                    )
                    for item in inbox.items
                ],
            )
        )
    print(f"({len(inbox.items)} messages since {since})")
    if inbox.meta.has_more:
        print("(older messages exist: raise --limit or narrow --hours)")
    return 0
