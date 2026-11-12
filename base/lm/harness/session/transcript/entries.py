from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from base.lm.harness.events import HarnessCause, Usage

# meiva:note
# The one vocabulary a harness CLI's own conversation record is read into, whatever the CLI is
# (Claude Code's transcript, Codex's thread items, Antigravity's `transcript_full.jsonl`). An adapter's
# parser turns each raw row into zero or more of these entries and nothing else; the projection in
# `projection.py` is the only place that decides what the timeline shows. Every entry kind below has a
# counterpart in at least two of the three records, and no field is named after one CLI's own.
# meiva:link base/lm/harness/docs/transcript-projection.md

MAIN_RECORD = "main"


# meiva:note
# Where an entry sits inside its own record, stated by the parser: `seq` is the record's own position
# (a line, an ordinal, a step index; holes are allowed) and `sub` orders the several entries one raw
# row yields (a planner step that thinks and calls two tools). It says nothing about records against
# each other and nothing about wall-clock time: the timeline is appended in the order entries arrive
# at the writer, so a command that finishes late is appended when it finishes, and a sub-agent's rows
# when they are read. An entry may arrive after one that sorts later in its own record (a result before
# its call), and the projection copes.
@dataclass(frozen=True, slots=True, order=True)
class Order:
    seq: int
    sub: int = 0


# meiva:note
# `key` makes a write idempotent: the same row read twice has the same key, and one row that yields
# several entries gives each its own. `record` names the conversation record the entry was read from
# (`MAIN_RECORD`, or a sub-agent's record id). `turn` is the CLI's own name for the turn the entry
# belongs to, or None for "the turn that is open in this record": an entry that carries one may belong
# to an earlier turn than the last one seen. `at` is a display timestamp (ISO-8601), never an order.
@dataclass(frozen=True, slots=True, kw_only=True)
class Entry:
    key: str
    order: Order
    record: str = MAIN_RECORD
    turn: str | None = None
    at: str | None = None


# meiva:note
# A message a person (or the host on a person's behalf) put into the conversation. `wire_id` is the id
# the CLI kept for it when we gave it one (Claude's row uuid, Codex's client id, Antigravity's
# `clientMessageId`), or None when the record cannot say; the projection then falls back to the text.
# `joined_running_turn` is True when the message was taken into a turn that was already running (a
# steer) and False when it opened a turn of its own.
@dataclass(frozen=True, slots=True, kw_only=True)
class HumanInput(Entry):
    wire_id: str | None
    text: str
    joined_running_turn: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentText(Entry):
    text: str
    message_ref: str | None = None
    complete: bool = True


# meiva:note
# What the model reasoned. `text` is empty when the record only says that reasoning happened (Codex
# keeps it encrypted); the projection shows no placeholder for it.
@dataclass(frozen=True, slots=True, kw_only=True)
class Reasoning(Entry):
    text: str
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolCall(Entry):
    call_id: str
    name: str
    tool_input: Mapping[str, object]


class ToolStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"
    ABORTED = "aborted"
    BACKGROUND = "background"


# meiva:note
# BACKGROUND: the call returned before its work finished (Antigravity's row that stays `RUNNING`,
# Claude's "running in the background" result); the work's own end arrives later as a `Notice`.
@dataclass(frozen=True, slots=True, kw_only=True)
class ToolResult(Entry):
    call_id: str
    content: str
    status: ToolStatus = ToolStatus.OK
    duration_ms: int | None = None


# meiva:note
# The record says which child record a call started. Entries of the child record carry
# `record=<child_record>`; the projection nests them under the call as soon as it knows the link, in
# whichever order the two records arrive.
@dataclass(frozen=True, slots=True, kw_only=True)
class SubagentLink(Entry):
    call_id: str
    child_record: str
    label: str | None = None


# meiva:note
# Something the CLI told the conversation by itself: a finished background task, a sub-agent's report,
# a timer. It opens a turn the CLI began on its own unless `opens_turn` is False (a sub-agent's report
# that the record files under a pseudo turn); `call_id` names the call whose work it reports when the
# record says (Claude's task notification, Antigravity's task id).
@dataclass(frozen=True, slots=True, kw_only=True)
class Notice(Entry):
    text: str
    cause: HarnessCause = HarnessCause.UNKNOWN
    call_id: str | None = None
    opens_turn: bool = True


@dataclass(frozen=True, slots=True, kw_only=True)
class Interrupt(Entry):
    pass


@dataclass(frozen=True, slots=True, kw_only=True)
class Compaction(Entry):
    summary: str | None = None


# meiva:note
# One model call's spend, named by the model message it belongs to so a message the record repeats on
# several rows (Claude writes one row per content block) is counted once. `reliable` is False for a
# message cut short, whose count is a placeholder. `model` is the model the message ran on when the record says
# (a sub-agent may run another model than the main agent), for pricing.
@dataclass(frozen=True, slots=True, kw_only=True)
class TokenUsage(Entry):
    message_ref: str
    usage: Usage
    reliable: bool = True
    model: str | None = None


class TurnEndReason(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"


# meiva:note
# The record itself closes a turn (Codex's `task_complete`, Claude's last assistant row of a turn).
# Antigravity's record has none, so its turn ends are signals the entries cannot give.
@dataclass(frozen=True, slots=True, kw_only=True)
class TurnEnd(Entry):
    reason: TurnEndReason = TurnEndReason.COMPLETED


@dataclass(frozen=True, slots=True, kw_only=True)
class Failure(Entry):
    message: str


class AnomalyKind(StrEnum):
    UNKNOWN_TYPE = "unknown_type"
    MISSING_FIELD = "missing_field"
    BAD_ROW = "bad_row"
    ORPHAN_RELEASED = "orphan_released"
    UNLINKED_RELEASED = "unlinked_released"


# meiva:note
# A row the parser could not use. An unknown type is skipped and reported once per type; a known type
# that lacks a field the projection needs is skipped and reported as an exception, and a row the reader
# could not decode (not JSON, not an object, invalid text, nested too deep, too large) is skipped and
# reported with the reason. None of them puts anything on the timeline. `detail` is the type name, the
# missing field or the reason. `ORPHAN_RELEASED` and `UNLINKED_RELEASED` are the projection's own: an item it held
# too long (a result whose call was never read, the output of a sub-agent whose link was never read) and then put on
# the timeline top-level; `detail` is the call id or the record.
@dataclass(frozen=True, slots=True, kw_only=True)
class Anomaly(Entry):
    anomaly: AnomalyKind
    detail: str


AnyEntry = (
    HumanInput
    | AgentText
    | Reasoning
    | ToolCall
    | ToolResult
    | SubagentLink
    | Notice
    | Interrupt
    | Compaction
    | TokenUsage
    | TurnEnd
    | Failure
    | Anomaly
)
