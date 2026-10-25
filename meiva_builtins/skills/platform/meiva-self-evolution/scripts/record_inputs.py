# meiva:note
# Immutable facts retained by one self-evolution dataset record.

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple, TypedDict

from audit import LeakPaths

Event = tuple[str, Mapping[str, object]]


class Inbound(TypedDict):
    source: str
    content: str
    is_broadcast: bool
    claimed: bool


class TranscriptRow(NamedTuple):
    type: str
    content: str


class InboundFact(NamedTuple):
    source: str
    content: str
    is_broadcast: bool
    claimed: bool


class AgentMeta(NamedTuple):
    spawner: str
    status: str | None
    last_message_text: str | None


class TaskReceipts(NamedTuple):
    note: int
    status: int
    results: int
    state_changes: int


# meiva:note
# Only facts the record and its counters consume. Conversions match the original consumers:
# missing numeric fields use zero, malformed numeric fields raise, and text fields use str.
# Every event keeps its name, including unknown events counted as evidence.
class EventFact(NamedTuple):
    name: str
    duration_s: float = 0.0
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    body: str = ""
    activation: str | None = None
    activation_skipped: bool = False


def _to_float(value: object) -> float:
    if isinstance(value, (int, float, str)):
        return float(value)
    raise TypeError(f"expected float or str, got {type(value).__name__}")


def _to_int(value: object) -> int:
    if isinstance(value, (int, float, str)):
        return int(value)
    raise TypeError(f"expected int or str, got {type(value).__name__}")


def event_facts(events: Sequence[Event]) -> tuple[EventFact, ...]:
    facts: list[EventFact] = []
    for name, payload in events:
        match name:
            case "turn_end":
                facts.append(
                    EventFact(
                        name,
                        duration_s=_to_float(payload.get("duration_seconds", 0)),
                    )
                )
            case "llm_usage":
                facts.append(
                    EventFact(
                        name,
                        model=str(payload.get("model", "")),
                        tokens_in=_to_int(payload.get("in_total", 0)),
                        tokens_out=_to_int(payload.get("out_total", 0)),
                    )
                )
            case "code":
                facts.append(EventFact(name, body=str(payload.get("body", ""))))
            case "plugin_activation":
                plugin, surface, identifier = (
                    payload.get(key) for key in ("plugin", "surface", "identifier")
                )
                if plugin and surface and identifier:
                    facts.append(EventFact(name, activation=f"{plugin}/{surface}/{identifier}"))
                else:
                    facts.append(EventFact(name, activation_skipped=True))
            case _:
                facts.append(EventFact(name))
    return tuple(facts)


# meiva:note
# The authoritative snapshot has no mappings or row objects. Raw HTTP/DB/checkpoint rows
# cross their field owners before construction; rendering creates fresh JSON containers.
@dataclass(frozen=True)
class RunInputs:
    agent_id: int
    week: str
    events: tuple[EventFact, ...]
    log_events: tuple[str, ...]
    inbounds: tuple[InboundFact, ...]
    meta: AgentMeta
    transcript: tuple[TranscriptRow, ...]
    skills_touched: tuple[str, ...]
    leak_paths: LeakPaths | None = None
    task_receipts: TaskReceipts | None = None


def inbound_facts(rows: Sequence[Inbound]) -> tuple[InboundFact, ...]:
    return tuple(
        InboundFact(row["source"], row["content"], row["is_broadcast"], row["claimed"])
        for row in rows
    )


def transcript_facts(rows: Sequence[Mapping[str, str]]) -> tuple[TranscriptRow, ...]:
    return tuple(TranscriptRow(row.get("type", ""), row.get("content", "")) for row in rows)
