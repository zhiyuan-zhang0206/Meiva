# meiva:note
# The wording of the framework context notes, as functions of the values they state. The note
# builders in `context_notes.py` read the turn's context (the database row, the machine
# identity, the clock, the workspace folder) and hand the facts here; nothing in this module
# reads the process. The timezone note states the IANA name plus the offset in force at `now`,
# so a zone with daylight saving is correct on the day the window is established.
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_EXEC_TIMEOUT_FRAMING = (
    "Your execute_code call has a hard wall-clock timeout of "
    "{timeout_s:.0f} seconds ({timeout_display}). "
    "If your code exceeds this, it will be killed."
)

_TIMEZONE_FRAMING = (
    "Conversation timezone: {name} (UTC{offset}). Conversation timestamps "
    "without an offset use this timezone. Tool output, files and source "
    "messages may use other timezones; honor any explicit offset. When you "
    "record a time anywhere outside this conversation (a file, a memory note, "
    "a message to another machine), write ISO-8601 with an offset instead."
)


@dataclass(frozen=True)
class Workspace:
    display: str
    document: str | None
    error: str | None


def _timeout_display(timeout_s: float) -> str:
    minutes = timeout_s / 60
    if minutes == int(minutes):
        n = int(minutes)
        return f"{n} minute" if n == 1 else f"{n} minutes"
    return f"{timeout_s:.0f} seconds"


def exec_timeout_text(timeout_s: float) -> str:
    return _EXEC_TIMEOUT_FRAMING.format(
        timeout_s=timeout_s, timeout_display=_timeout_display(timeout_s)
    )


def timezone_text(name: str, now: datetime) -> str:
    raw = now.strftime("%z")
    return _TIMEZONE_FRAMING.format(name=name, offset=f"{raw[:3]}:{raw[3:]}")


def label_from_row(row: tuple[object, ...] | None) -> str | None:
    if row is None or not row[0]:
        return None
    return " ".join(str(row[0]).split())


def machine_name_text(name: str) -> str | None:
    return " ".join(name.split()) or None


def workspace_display(workspace: Path, home: Path) -> str:
    try:
        return f"~/{workspace.relative_to(home)}"
    except ValueError:
        return str(workspace)


def agent_id_text(
    agent_id: int, *, label: str | None, machine: str | None, workspace: Workspace | None
) -> str:
    clauses: list[str] = []
    if label:
        clauses.append(f"label: {label}")
    if machine:
        clauses.append(f"machine: {machine}")
    detail = f" ({', '.join(clauses)})" if clauses else ""
    tail = ""
    if workspace is not None:
        tail = f" Your workspace is {workspace.display}."
        if workspace.error is not None:
            tail += (
                f"\nThe shared context file could not be read: {workspace.error}. "
                "Repair it in your workspace."
            )
        elif workspace.document:
            tail += (
                "\n\nCurrent shared context displayed beside your messages:\n" + workspace.document
            )
    return f"Your Agent ID is {agent_id}{detail}.{tail}"
