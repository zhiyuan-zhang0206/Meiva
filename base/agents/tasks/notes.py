"""The stamped-note line appended to an `agent_tasks.results` column — one
implementation for the two writers.

An agent's task notes are appended by the SDK task registry
(`meiva_builtins/plugins/meiva_fleet/task_registry.py`) and by the gateway when a
machine pause reassigns a task to the drain owner
(`gateway/cluster/machine_pause.py`). Both build the same line, and both used
to build it by hand from `datetime.now(UTC).astimezone()` — the *writing
machine's* local timezone. A fleet spans machines, so one task's notes arrived
stamped in several different timezones, none of them marked, sitting in one
column of text an agent reads top to bottom.

Kept here so the two writers cannot drift apart, and so the stamp is the same
agent-facing representation as every other timestamp an agent sees:
`base.clock.Clock.format_timestamp`, in the cluster timezone the agent is told
about once by its standing context note.
"""

from __future__ import annotations

from datetime import datetime

from base.clock import Clock


# meiva:note
# `note` as one stamped, newline-terminated line for `agent_tasks.results`, stamped `now`.
def task_note_line(note: str, *, clock: Clock, now: datetime) -> str:
    return f"{clock.format_timestamp(now)} {note}\n"
