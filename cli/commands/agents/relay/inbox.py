# meiva:note
# What the relay learns from one read of the lease: the pending rows with their bodies, the
# delivery budget the lease snapshotted, and whether the lease is still open.
#
# The shell turns the database rows into these values once per read. Every later decision
# (what is due, whether to stop, which rows the merge window may hold) is a function of one
# `InboxSnapshot`, so a decision never reads the database or the clock.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel

from base.agents.impersonation.status import OPEN, ImpersonationStatus

__all__ = [
    "DeliveryBudget",
    "EndVerdict",
    "EndedLease",
    "InboxMessage",
    "InboxSnapshot",
    "LeaseRef",
    "due_ids",
    "end_verdict",
    "routine_ids",
    "terminal_record",
]

_TERMINAL = frozenset(ImpersonationStatus) - frozenset(OPEN)


# meiva:note
# Who the relay speaks for in its texts: the agent, and the lease handle it names in the ACK
# command. A session number (an int) names the lease together with the agent; the lease's
# UUID names it alone.
@dataclass(frozen=True)
class LeaseRef:
    agent_id: int
    lease_ref: int | UUID


# meiva:note
# The delivery budget a lease snapshotted at request: each pushed row gets this many
# attempts, each with this many seconds to be acknowledged.
@dataclass(frozen=True)
class DeliveryBudget:
    ack_window_seconds: int
    max_delivery_attempts: int


# meiva:note
# One durable inbox row crossing the host wake boundary with its body.
@dataclass(frozen=True)
class InboxMessage:
    id: int
    kind: str
    source: str
    content: str
    delivery_attempts: int
    delivery_due: bool


# meiva:note
# The immutable record the lease wrote when it ended, from which the terminal notice is
# worded. Keys beyond these six belong to the notice's other readers and are ignored.
class EndedLease(BaseModel, frozen=True):
    session_id: int | UUID
    lease_id: str
    agent_id: int
    status: str
    reason: str | None
    ended_at: str


# meiva:note
# Pending rows plus the lease facts the delivery loop needs.
#
# `messages` carries each row's full body, in id order: the push envelope is built from it,
# so the host never has to read the inbox itself. `routine_ids` is the subset of the ids the
# merge window may coalesce (not a user chat, not a cancel, not a reminder); anything outside
# it pushes immediately. `batch_window` is the lease's merge window in seconds, 0 for none.
# `start_message` is the native agent's briefing, empty only for a lease accepted before the
# push protocol shipped. `ended` is the lease's terminal record, None until it ends and for
# a lease that predates the record.
@dataclass(frozen=True)
class InboxSnapshot:
    messages: tuple[InboxMessage, ...]
    expires_at: datetime
    status: ImpersonationStatus
    routine_ids: frozenset[int]
    batch_window: float
    start_message: str
    budget: DeliveryBudget
    end_reason: str | None
    ended: EndedLease | None

    @property
    def message_ids(self) -> frozenset[int]:
        return frozenset(message.id for message in self.messages)

    @property
    def active(self) -> bool:
        return self.status == ImpersonationStatus.ACTIVE

    # meiva:note
    # A lease that is not active exposes no inbox: its status and, once ended, the reason and
    # the terminal record are all there is to read.
    @classmethod
    def without_inbox(
        cls,
        status: ImpersonationStatus,
        expires_at: datetime,
        *,
        end_reason: str | None,
        ended: EndedLease | None,
        budget: DeliveryBudget,
    ) -> InboxSnapshot:
        return cls(
            messages=(),
            expires_at=expires_at,
            status=status,
            routine_ids=frozenset(),
            batch_window=0.0,
            start_message="",
            budget=budget,
            end_reason=end_reason,
            ended=ended,
        )

    def message(self, message_id: int) -> InboxMessage:
        for message in self.messages:
            if message.id == message_id:
                return message
        raise ValueError(f"inbox row {message_id} is not in this snapshot")


# meiva:note
# What the relay does with a snapshot whose lease may have ended.
class EndVerdict(Enum):
    CONTINUE = "continue"
    STOP_SILENT = "stop_silent"
    STOP_WITH_NOTICE = "stop_with_notice"


# meiva:note
# Whether the relay keeps going, and if it stops whether it words the end.
#
# The releasing controller already knows its lease ended, so a release is never announced. A
# host that submits its own ended notice (Codex, Antigravity) is not told twice.
def end_verdict(status: ImpersonationStatus, *, notify_terminal: bool) -> EndVerdict:
    if status not in _TERMINAL:
        return EndVerdict.CONTINUE
    if not notify_terminal or status == ImpersonationStatus.RELEASED:
        return EndVerdict.STOP_SILENT
    return EndVerdict.STOP_WITH_NOTICE


# meiva:note
# The rows the merge window may coalesce.
#
# User chats, cancels and renewal reminders are never routine: they push immediately. Every
# other arrival (peer/system notification, watcher wake, schedule trigger, page message) may
# wait out the configured window.
def routine_ids(messages: Sequence[InboxMessage]) -> frozenset[int]:
    return frozenset(
        message.id
        for message in messages
        if message.kind not in ("cancel", "reminder")
        and not (message.kind == "chat" and message.source == "user")
    )


# meiva:note
# The rows whose next delivery attempt is due: not waiting out an ACK window, with budget
# left, and either all first attempts or all redeliveries. Due retries precede fresh rows so
# arrivals cannot starve them.
def due_ids(snapshot: InboxSnapshot, *, redelivery: bool) -> frozenset[int]:
    return frozenset(
        message.id
        for message in snapshot.messages
        if message.delivery_due
        and message.delivery_attempts < snapshot.budget.max_delivery_attempts
        and (message.delivery_attempts > 0) == redelivery
    )


# meiva:note
# The record the terminal notice describes: the lease's immutable one, or for a lease without one
# (a synthetic or pre-migration lease) the supplied lease scope with the historical time disclosed
# as unavailable.
def terminal_record(snapshot: InboxSnapshot, ref: LeaseRef) -> EndedLease:
    match snapshot.ended:
        case EndedLease() as record:
            return record
        case None:
            return EndedLease(
                session_id=ref.lease_ref,
                lease_id=str(ref.lease_ref),
                agent_id=ref.agent_id,
                status=snapshot.status.value,
                reason=snapshot.end_reason,
                ended_at="unknown (legacy record)",
            )
