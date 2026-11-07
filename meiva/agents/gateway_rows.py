# meiva:note
# Gateway JSON -> SDK types: the one place that turns a reply row into an
# `AgentRow`, `Machine`, `Neighbor` or `OpenTasksHint`. The gateway client
# answers `dict[str, object]`, so every field arrives here as `object`; these
# readers decide what a row may contain, and a wrong-typed field raises with
# its name instead of flowing on as a wrong-typed value.
# meiva:link meiva/agents/docs/agents.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from base.api_contracts.agent_contract import AgentStatus, ShellSessionKillTiming
from base.host.boundary.json_values import is_object_dict, is_object_list
from meiva.agents import row_text
from meiva.agents.contracts import HarnessAttachment, Machine, Neighbor, ShellSessionsKill

__all_for_meiva__ = [
    "AgentDirectoryPage",
    "AgentRow",
    "OpenTaskRow",
    "OpenTasksHint",
    "harness_attachment_from_dict",
    "machines_from_rows",
    "neighbor_from_row",
    "object_rows",
    "open_tasks_from_dict",
    "row_from_dict",
    "shell_sessions_from_dict",
]


@dataclass
class AgentRow:
    agent_id: int
    label: str | None
    status: AgentStatus
    spawner: str
    fork_source_agent_id: int | None
    machine: str
    spawned_at: datetime
    # meiva:note
    # `last_active_at` is the agent's real-activity clock (every completed LLM
    # turn) — the value triage surfaces should use for "is it alive". The API
    # also carries `last_inbound_at` ("when did anyone last talk to it") since
    # the two diverge during long single turns (issue #183).
    last_active_at: datetime
    last_inbound_at: datetime
    heartbeat_paused_until: datetime | None
    # meiva:note
    # How this agent's external harness session ended (task #5616). None means
    # "still running, or never recorded" — including every built-in meiva agent,
    # which has no CLI child at all; it never means "exited cleanly". 0 = the child
    # exited normally, non-zero = it exited abnormally. `harness_exit_at` is when that
    # status was read (the session's retirement, after the child was reaped).
    harness_exit_code: int | None = None
    harness_exit_at: datetime | None = None

    def __str__(self) -> str:
        label_part = f"{self.label} " if self.label else ""
        parts = [f"#{self.agent_id} {label_part} {self.status.value}"]
        parts.append(f"machine={self.machine}")
        parts.append(f"spawned={_relative_time(self.spawned_at)}")
        if self.last_active_at != self.spawned_at:
            parts.append(f"last_active={_relative_time(self.last_active_at)}")
        return "  ".join(parts)


@dataclass
class AgentDirectoryPage:
    """One page, newest agent IDs first. Pass `next_cursor` as `before_id`
    with the same filters to read the next page; None means no more results.
    """

    agents: list[AgentRow]
    next_cursor: int | None


def _relative_time(dt: datetime) -> str:
    """Convert a datetime into a human-readable relative time string."""
    from meiva.sdk_surface.settings import clock

    current_clock = clock()
    zone = current_clock.zone()
    now = current_clock.now().astimezone(zone)
    return row_text.relative_age(dt.astimezone(zone), now=now)


@dataclass
class OpenTaskRow:
    """One open task a terminated agent still owns: its id, title, status and
    last update time."""

    id: int
    title: str
    status: str
    updated_at: datetime

    def __str__(self) -> str:
        return f"#{self.id} | {self.title} | {self.status} | {_relative_time(self.updated_at)}"


@dataclass
class OpenTasksHint:
    """The open tasks a terminated agent still owns as it goes down: the total
    `count`, at most the five most recently updated `tasks`, and `more` — how
    many beyond those five remain."""

    count: int
    tasks: list[OpenTaskRow]
    more: int

    def __str__(self) -> str:
        lines = [f"{self.count} open task(s):", *(str(task) for task in self.tasks)]
        if self.more:
            lines.append(f"... and {self.more} more")
        return "\n".join(lines)


def machines_from_rows(rows: Sequence[object]) -> list[Machine]:
    """Machine rows -> Machine dataclasses."""
    return [
        Machine(
            name=text(row["name"], "name"),
            description=optional_text(row.get("description"), "description"),
            live=boolean(row["live"], "live"),
        )
        for row in object_rows(rows, "machines")
    ]


# meiva:note
# The gateway client hands back `dict[str, object]` rows, so every field reaches
# this module as `object`. These readers are the one place that decides what a
# row may contain: a field of the wrong type raises with its name instead of
# flowing on as a wrong-typed value.
def text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string, got {type(value).__name__}")
    return value


def optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return text(value, name)


def integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, got {type(value).__name__}")
    return value


def optional_integer(value: object, name: str) -> int | None:
    if value is None:
        return None
    return integer(value, name)


def number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number, got {type(value).__name__}")
    return float(value)


def boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a boolean, got {type(value).__name__}")
    return value


def timestamp(value: object, name: str) -> datetime:
    return datetime.fromisoformat(text(value, name))


def optional_timestamp(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return timestamp(value, name)


def object_rows(value: object, name: str) -> list[Mapping[str, object]]:
    """A JSON array of objects; one wrong entry refuses the whole row list."""
    if not is_object_list(value):
        raise TypeError(f"{name} must be a list, got {type(value).__name__}")
    rows: list[Mapping[str, object]] = []
    for index, row in enumerate(value):
        if not is_object_dict(row):
            raise TypeError(f"{name}[{index}] must be an object, got {type(row).__name__}")
        rows.append(row)
    return rows


def neighbor_from_row(row: Mapping[str, object]) -> Neighbor:
    return Neighbor(
        agent_id=integer(row["agent_id"], "agent_id"),
        label=optional_text(row.get("label"), "label"),
        status=AgentStatus(text(row["status"], "status")),
        depth=integer(row["depth"], "depth"),
        score=number(row["score"], "score"),
    )


def harness_attachment_from_dict(data: object) -> HarnessAttachment | None:
    if data is None:
        return None
    if not is_object_dict(data):
        raise TypeError(f"attached must be an object, got {type(data).__name__}")
    return HarnessAttachment(
        agent_id=integer(data["agent_id"], "agent_id"),
        controller_pid=integer(data["controller_pid"], "controller_pid"),
        expires_at=optional_text(data.get("expires_at"), "expires_at"),
    )


def shell_sessions_from_dict(data: object) -> ShellSessionsKill | None:
    if data is None:
        return None
    if not is_object_dict(data):
        raise TypeError(f"shell_sessions must be an object, got {type(data).__name__}")
    killed = data.get("killed")
    if not is_object_list(killed):
        raise TypeError(f"shell_sessions.killed must be a list, got {type(killed).__name__}")
    # meiva:note
    # A `when` outside the shared timing enum is an error, not a guess — including a value that is
    # not a string at all, which the SDK has always answered with this ValueError.
    # `# noqa: TRY004` is deliberate: a wrong *type* here is not a caller mistake but a wire answer
    # outside the enum, and `ShellSessionKillTiming` is the type that owns that refusal
    # (`test_terminate_rejects_unknown_shell_cleanup_timing` pins ValueError for "later", "" and None).
    when = data.get("when")
    if not isinstance(when, str):
        raise ValueError(f"{when!r} is not a valid ShellSessionKillTiming")  # noqa: TRY004
    return ShellSessionsKill(
        when=ShellSessionKillTiming(when),
        killed=[integer(entry, "killed") for entry in killed],
    )


def row_from_dict(data: Mapping[str, object]) -> AgentRow:
    """One agent row → AgentRow dataclass."""
    return AgentRow(
        agent_id=integer(data["agent_id"], "agent_id"),
        label=optional_text(data.get("label"), "label"),
        status=AgentStatus(text(data["status"], "status")),
        spawner=text(data["spawner"], "spawner"),
        fork_source_agent_id=optional_integer(data["fork_source_agent_id"], "fork_source_agent_id"),
        machine=text(data["machine"], "machine"),
        spawned_at=timestamp(data["spawned_at"], "spawned_at"),
        last_active_at=timestamp(data["last_active_at"], "last_active_at"),
        last_inbound_at=timestamp(data["last_inbound_at"], "last_inbound_at"),
        heartbeat_paused_until=optional_timestamp(
            data.get("heartbeat_paused_until"), "heartbeat_paused_until"
        ),
        harness_exit_code=optional_integer(data.get("harness_exit_code"), "harness_exit_code"),
        harness_exit_at=optional_timestamp(data.get("harness_exit_at"), "harness_exit_at"),
    )


def open_tasks_from_dict(data: object) -> OpenTasksHint | None:
    """Open-tasks dict → OpenTasksHint; null passes through."""
    if data is None:
        return None
    if not is_object_dict(data):
        raise TypeError(f"open_tasks must be an object, got {type(data).__name__}")
    return OpenTasksHint(
        count=integer(data["count"], "count"),
        tasks=[
            OpenTaskRow(
                id=integer(row["id"], "id"),
                title=text(row["title"], "title"),
                status=text(row["status"], "status"),
                updated_at=timestamp(row["updated_at"], "updated_at"),
            )
            for row in object_rows(data.get("tasks"), "tasks")
        ],
        more=integer(data["more"], "more"),
    )
