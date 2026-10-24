# meiva:note
# Which agent a label names, decided one directory page at a time.
#
# `list_agents` searches by label substring, so a page can hold agents whose label only
# contains the one wanted; only an exact label counts. The shell reads a page, passes it
# here, and follows the cursor while the plan says `NextPage`.
# meiva:link schedules/README.md

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "Found",
    "ListedAgent",
    "NextPage",
    "NotFound",
    "ReportRecipient",
    "Verdict",
    "plan_directory_page",
    "report_recipient",
]


@dataclass(frozen=True)
class ListedAgent:
    agent_id: int
    label: str | None
    terminated: bool


@dataclass(frozen=True)
class Found:
    agent_id: int
    terminated: bool


@dataclass(frozen=True)
class NextPage:
    cursor: int


@dataclass(frozen=True)
class NotFound:
    pass


Verdict = Found | NextPage | NotFound


# meiva:note
# The first agent on the page with exactly this label wins; a page without one continues at the
# cursor, and the last page without one ends the search.
def plan_directory_page(
    agents: tuple[ListedAgent, ...], next_cursor: int | None, label: str
) -> Verdict:
    for agent in agents:
        if agent.label == label:
            return Found(agent.agent_id, agent.terminated)
    if next_cursor is None:
        return NotFound()
    return NextPage(next_cursor)


# meiva:note
# Who a schedule reports to: the agent id an operator named in `env_name`, or else whichever
# agent carries `label`. The id is validated when the recipient is built, at the entry, so a
# malformed value stops the schedule at its start instead of at its first failure report.
@dataclass(frozen=True)
class ReportRecipient:
    schedule: str
    env_name: str
    label: str
    agent_id: int | None


# meiva:note
# `configured` is the raw text of the operator variable. Blank means "not set"; anything else
# must be a positive integer and never falls back to the label lookup.
def report_recipient(
    schedule: str, env_name: str, label: str, configured: str | None
) -> ReportRecipient:
    if configured is None or not configured.strip():
        return ReportRecipient(schedule, env_name, label, None)
    try:
        agent_id = int(configured)
    except ValueError as exc:
        raise RuntimeError(f"{env_name} must be a numeric agent id") from exc
    if agent_id <= 0:
        raise RuntimeError(f"{env_name} must be a positive agent id")
    return ReportRecipient(schedule, env_name, label, agent_id)
