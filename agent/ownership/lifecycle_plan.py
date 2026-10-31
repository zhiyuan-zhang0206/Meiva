# meiva:note
# The pure decisions of the hosted lifecycle: which claimed command may run, what a
# restart leaves for the agent to read, which shell sessions a terminate may take with
# it, and what closing a turn must do. `hosted.py` runs the transactions and executes
# these values.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

from base.agents.messages.kwargs import NoteTag
from base.agents.messages.lifecycle_text import restart_resume_note
from base.native_process.runtime_incarnation import RuntimeIncarnation

LifecycleKind = Literal["restart", "terminate"]


# meiva:note
# What a committed lifecycle command leaves to do after the commit. `shell_cutoff` is the
# exclusive shell-ID bound of the terminate's requested shell cleanup, or None when no
# closed terminate asked for it.
@dataclass(frozen=True)
class AppliedLifecycle:
    kind: LifecycleKind
    agent_id: int
    shell_cutoff: int | None


# meiva:note
# One committed termination; no recovery input is created.
class ReapedCorpse(NamedTuple):
    agent_id: int


# meiva:note
# The caller must stop without changing a replacement runtime's state.
class RuntimeOwnershipLostError(RuntimeError):
    pass


# meiva:note
# One crash's stamp outcome: `applied` is the CAS result.
@dataclass(frozen=True)
class TurnFatalStamp:
    applied: bool


# meiva:note
# The settle boundary's facts about one closed turn: `stamp` is the crash stamp (None when
# the turn did not crash or its stamp failed), and `settled` says the settle wrote the row
# idling, gated on fully discharged turn resources.
@dataclass(frozen=True)
class TurnSettlement:
    stamp: TurnFatalStamp | None
    settled: bool


@dataclass(frozen=True)
class ResumeNote:
    content: str
    source: str
    note_tag: str


@dataclass(frozen=True)
class TurnClose:
    stamp_fatal: bool
    settle: bool


# meiva:note
# The claim filters on these two kinds; any other kind reaching the apply is a defect.
def lifecycle_kind(kind: str) -> LifecycleKind:
    match kind:
        case "restart" | "terminate":
            return kind
        case _:
            raise ValueError(f"not an executable lifecycle command: {kind}")


# meiva:note
# The one pending note that continues a turn the restart ended unfinished; an idle agent
# gets none.
def resume_note(
    source: str,
    *,
    resume_turn: bool,
    message: str | None = None,
) -> ResumeNote | None:
    if not resume_turn:
        return None
    return ResumeNote(
        content=restart_resume_note(source, message),
        source=source,
        note_tag=NoteTag.LIFECYCLE_RESTART.value,
    )


# meiva:note
# The exclusive shell-ID cutoff for the post-commit cleanup, or None when no closed
# terminate asked for the agent's shell sessions to go with it. `session_index` is the
# next shell ID read under the agent row lock.
def shell_cutoff(kill_flags: Sequence[bool], session_index: int) -> int | None:
    return session_index if any(kill_flags) else None


# meiva:note
# A crash stamps the corpse marker first, because its CAS needs `status='running'`; the
# settle to idling is skipped once the host process is exiting.
def turn_close(*, exited: bool, crashed: bool) -> TurnClose:
    return TurnClose(stamp_fatal=crashed, settle=not exited)


# meiva:note
# An unowned row (no generation and no owner) accepts the missing token, and an owned row
# requires the caller to hold the row's exact generation and owner while running or idling.
def owns_inbound_row(
    row: Sequence[object] | None,
    *,
    incarnation: RuntimeIncarnation | None,
) -> bool:
    if row is None:
        return False
    if len(row) != 3:
        raise ValueError(
            f"the owner lock selects (generation, owner, status); got a row of {len(row)} columns"
        )
    generation, owner, status = row
    if incarnation is None:
        return generation is None and owner is None
    return (
        generation == incarnation.generation
        and owner == incarnation.owner
        and status in ("running", "idling")
    )
