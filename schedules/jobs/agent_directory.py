# meiva:note
# Finding, waking and messaging the agents a schedule works through, over the SDK's directory.
#
# `find_agent` walks `meiva.agents.list_agents` page by page and lets `plan_directory_page`
# say whether a page holds the label, continues, or ends the search. `ensure_agent` acts on the
# answer: a terminated agent is resurrected with the prompt, a live one is messaged, and a
# missing one is spawned under a fresh idempotency key. The calls are the SDK's own and go
# through the process's gateway context.
# meiva:link schedules/README.md

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, assert_never
from uuid import uuid4

import meiva
from meiva.agents import AgentRow, AgentStatus
from schedules.plans.directory_plan import (
    Found,
    ListedAgent,
    NextPage,
    NotFound,
    ReportRecipient,
    plan_directory_page,
)

__all__ = [
    "AgentDispatch",
    "ensure_agent",
    "find_agent",
    "iter_agents",
    "notify_report_agent",
    "report_agent_id",
]


# meiva:note
# What `ensure_agent` did, so a caller can report it.
@dataclass(frozen=True)
class AgentDispatch:
    agent_id: int
    action: Literal["spawned", "resurrected", "messaged"]


def find_agent(label: str) -> Found | None:
    cursor: int | None = None
    while True:
        page = meiva.agents.list_agents(scope="all", query=label, before_id=cursor)
        listed = tuple(
            ListedAgent(row.agent_id, row.label, row.status == AgentStatus.TERMINATED)
            for row in page.agents
        )
        verdict = plan_directory_page(listed, page.next_cursor, label)
        match verdict:
            case Found():
                return verdict
            case NextPage(cursor=next_cursor):
                cursor = next_cursor
            case NotFound():
                return None
            case _:
                assert_never(verdict)


def ensure_agent(label: str, prompt: str) -> AgentDispatch:
    found = find_agent(label)
    if found is None:
        agent_id = meiva.agents.spawn(prompt=prompt, label=label, idempotency_key=str(uuid4()))
        return AgentDispatch(agent_id, "spawned")
    if found.terminated:
        meiva.agents.resurrect(found.agent_id, prompt)
        return AgentDispatch(found.agent_id, "resurrected")
    meiva.agents.send_message(found.agent_id, prompt)
    return AgentDispatch(found.agent_id, "messaged")


def report_agent_id(recipient: ReportRecipient) -> int:
    if recipient.agent_id is not None:
        return recipient.agent_id
    found = find_agent(recipient.label)
    if found is None:
        raise RuntimeError(f"no report agent labelled {recipient.label!r} is available")
    return found.agent_id


def notify_report_agent(recipient: ReportRecipient, message: str) -> None:
    try:
        meiva.agents.send_message(report_agent_id(recipient), message)
    except Exception as exc:
        print(f"{recipient.schedule} could not notify the P0 lead: {exc}")
        raise


# meiva:note
# Every agent of the scope and query in directory order, following the cursor. Callers that
# decide per agent (not per label) read rows from here; `limit` is the page size.
def iter_agents(
    *, scope: Literal["live", "terminated", "all"], query: str = "", limit: int = 100
) -> Iterator[AgentRow]:
    cursor: int | None = None
    while True:
        page = meiva.agents.list_agents(scope=scope, query=query, before_id=cursor, limit=limit)
        yield from page.agents
        if page.next_cursor is None:
            return
        cursor = page.next_cursor
