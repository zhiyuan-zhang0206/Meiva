from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from base.host.boundary.json_values import is_object_dict, is_object_list

# meiva:note
# The work an external CLI keeps running or has queued outside any turn: a background command, a
# background sub-agent, a timer or cron job the model set. The vocabulary is shared by the three
# adapters and by the bridge and the host, so neither names a harness: an adapter answers "what is
# pending" and the rest of the system only asks whether the answer is empty. A session holding
# any of it is not idle, whatever its turn state says, because closing its CLI would drop that work
# silently — see the 2026-11-11 decision.
# meiva:link base/lm/harness/docs/background-work.md


class BackgroundKind(StrEnum):
    COMMAND = "command"
    AGENT = "agent"
    TURN = "turn"
    CRON = "cron"
    WAKEUP = "wakeup"
    SCHEDULE = "schedule"


_DESCRIPTION_LIMIT = 120


# meiva:note
# One line the model can recognise its own work by: whitespace collapsed and cut, because the
# description is the only thing the agent is told when the work is lost. `until` is the wall-clock
# second (epoch) after which the item no longer counts as pending: a one-shot timer that has come
# due, a recurring cron past its seven-day expiry. None means the adapter knows no end.
@dataclass(frozen=True, slots=True)
class BackgroundItem:
    kind: BackgroundKind
    description: str
    until: float | None = None

    def pending_at(self, now: float) -> bool:
        return self.until is None or self.until > now


@dataclass(frozen=True, slots=True)
class BackgroundWork:
    items: tuple[BackgroundItem, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.items)

    def pending_at(self, now: float) -> BackgroundWork:
        return BackgroundWork(tuple(item for item in self.items if item.pending_at(now)))


# meiva:note
# Who answers the read-only question "is any background work pending right now". A session and the
# bridge that owns it both answer; a fake that does not has nothing pending. The check is structural
# (`isinstance`) rather than a member of the session protocol, as for foreground steering, so an
# adapter that cannot tell stays a valid session and the caller never branches on the harness name.
@runtime_checkable
class BackgroundWorkReporting(Protocol):
    def background_work(self) -> BackgroundWork: ...


# meiva:note
# What a restarted CLI lost, handed over once. `take_lost_background` returns the items recorded
# as pending when the previous process went away (or lost to an in-session process replacement)
# and forgets them, so the note that tells the agent is written exactly once.
@runtime_checkable
class LostBackgroundReporting(Protocol):
    def take_lost_background(self) -> tuple[BackgroundItem, ...]: ...


def describe(text: str, *, limit: int = _DESCRIPTION_LIMIT) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


_KIND_LABEL = {
    BackgroundKind.COMMAND: "background command",
    BackgroundKind.AGENT: "background sub-agent",
    BackgroundKind.TURN: "turn the CLI started by itself",
    BackgroundKind.CRON: "scheduled cron job",
    BackgroundKind.WAKEUP: "scheduled wakeup",
    BackgroundKind.SCHEDULE: "scheduled timer or job",
}


# meiva:note
# The system note the first input after a restart carries. It names what was lost and states that
# nothing will run or report back, and leaves the decision to the agent: rebuilding a timer the
# agent no longer needs would be worse than telling it the timer is gone.
def lost_work_note(items: Sequence[BackgroundItem], *, harness: str, now: float) -> str | None:
    lost = [item for item in items if item.pending_at(now)]
    if not lost:
        return None
    lines = [
        f"System note: the {harness} CLI process for this session was restarted. The background "
        "work below was still pending and is lost: it will not run or report back. Recreate "
        "whatever you still need.",
        *(f"- {_KIND_LABEL[item.kind]}: {item.description}" for item in lost),
    ]
    return "\n".join(lines)


def encode_items(items: Sequence[BackgroundItem]) -> list[dict[str, object]]:
    return [
        {"kind": item.kind.value, "description": item.description, "until": item.until}
        for item in items
    ]


def decode_items(raw: object, *, source: str) -> tuple[BackgroundItem, ...]:
    if not is_object_list(raw):
        raise TypeError(f"{source}: background must be a list")
    items: list[BackgroundItem] = []
    for entry in raw:
        if not is_object_dict(entry):
            raise TypeError(f"{source}: a background entry is not an object")
        kind, description, until = entry.get("kind"), entry.get("description"), entry.get("until")
        if not isinstance(kind, str) or not isinstance(description, str):
            raise TypeError(f"{source}: a background entry needs a kind and a description")
        if until is not None and (isinstance(until, bool) or not isinstance(until, int | float)):
            raise TypeError(f"{source}: a background entry's until must be a number or null")
        items.append(
            BackgroundItem(
                kind=BackgroundKind(kind),
                description=description,
                until=None if until is None else float(until),
            )
        )
    return tuple(items)
