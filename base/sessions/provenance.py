# meiva:note
# A dispatch block names the agent that dispatched a worker, the spawn chain behind it, the task the
# worker serves and the agent its result returns to. The launcher generates it; the worker only
# reads it. The renderer is pure so the exact bytes are testable without a cluster. Two surfaces
# share the data: a supervised launch leads with the block and writes the same bytes at the top of
# the task file, while a takeover briefing carries the three-line form and creates no files. Nothing
# here queries the task registry: a launcher runs as a short-lived subprocess where the task SDK is
# not initialized, and a launch must not block on a lookup, so the caller passes the task facts it
# already has.
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

MARKER = "## Who dispatched you"
MAX_BLOCK_CHARS = 800
# meiva:note
# A label is decoration: bounding it keeps every other field, including report_to and must_report,
# inside the block budget. No real label is near this (the longest in the cluster is 67 characters);
# the bound exists so a self-chosen long label can never truncate the return address.
MAX_LABEL_CHARS = 80
MAX_TAKEOVER_CHARS = 400
_MUST_REPORT = (
    "must_report: for_task id + absolute path of every deliverable + a one-line conclusion"
)


@dataclass(frozen=True)
class DispatchProvenance:
    parent_agent_id: int | None
    parent_label: str | None
    spawn_chain: tuple[str, ...]
    dispatched_at: dt.datetime
    why: str
    task_id: int | None = None
    task_label: str | None = None
    priority: str | None = None


def _line(text: str) -> str:
    return " ".join(text.split()).replace('"', "'")


def _why(prov: DispatchProvenance) -> str:
    return _line(prov.why) or "unspecified"


def _dispatched_by(prov: DispatchProvenance, *, compact: bool) -> str:
    if prov.parent_agent_id is None:
        return "dispatched_by: unknown (no agent identity in the launching process)"
    head = f"dispatched_by: agent {prov.parent_agent_id}"
    label = _line(prov.parent_label or "")[:MAX_LABEL_CHARS]
    if label:
        head += f' "{label}"'
    if prov.spawn_chain and not compact:
        head += f" (spawn_chain: {' > '.join(prov.spawn_chain)})"
    return head


def _for_task(prov: DispatchProvenance, *, compact: int) -> str:
    if prov.task_id is None:
        return "for_task: none (ad-hoc)"
    head = f"for_task: {prov.task_id}"
    label = _line(prov.task_label or "")
    if label and compact < 3:
        head += f' "{label}"'
    if prov.priority and compact < 2:
        head += f" ({_line(prov.priority)})"
    return head


def _render(prov: DispatchProvenance, *, compact: int) -> str:
    lines = [
        f"{MARKER} (generated - quote it when you report; do not edit)",
        _dispatched_by(prov, compact=compact > 0),
        _for_task(prov, compact=compact),
        f"dispatched_at: {prov.dispatched_at.astimezone(dt.UTC).isoformat(timespec='minutes')}",
        f"why_you: {_why(prov)}",
        f"report_to: agent {prov.parent_agent_id}"
        if prov.parent_agent_id
        else "report_to: unknown",
        _MUST_REPORT,
    ]
    return "\n".join(lines)


def _degrade(prov: DispatchProvenance, *, why: str) -> DispatchProvenance:
    return DispatchProvenance(
        parent_agent_id=prov.parent_agent_id,
        parent_label=prov.parent_label,
        spawn_chain=(),
        dispatched_at=prov.dispatched_at,
        why=why,
        task_id=prov.task_id,
    )


# meiva:note
# Degradation is ordered and visible: the spawn chain goes first, then task detail, then the task
# label, and only then is the operator's reason shortened with a visible ellipsis. The tail lines
# (report_to, must_report) always survive: every field above them is bounded or droppable, so the
# block never loses the return address to truncation.
def render(prov: DispatchProvenance) -> str:
    for compact in range(4):
        text = _render(prov, compact=compact)
        if len(text) <= MAX_BLOCK_CHARS:
            return text
    bare = _render(_degrade(prov, why="omitted (the briefing text was too long)"), compact=3)
    spare = MAX_BLOCK_CHARS - len(bare)
    if spare <= 8:
        return bare
    shortened = _degrade(prov, why=_why(prov)[: spare - 1].rstrip() + "\u2026")
    return _render(shortened, compact=3)


def render_takeover(prov: DispatchProvenance) -> str:
    parent = f"agent {prov.parent_agent_id}" if prov.parent_agent_id else "unknown"
    label = _line(prov.parent_label or "")
    requested_by = f"requested_by: {parent}" + (f' "{label}"' if label else "")
    text = "\n".join([requested_by, f"why: {_why(prov)}", f"return_to: {parent}"])
    if len(text) <= MAX_TAKEOVER_CHARS:
        return text
    fixed = len(text) - len(_why(prov))
    budget = max(MAX_TAKEOVER_CHARS - fixed - 1, 0)
    shortened = _why(prov)[:budget].rstrip() + "\u2026"
    return "\n".join([requested_by, f"why: {shortened}", f"return_to: {parent}"])


def has_marker(text: str) -> bool:
    return MARKER in text
