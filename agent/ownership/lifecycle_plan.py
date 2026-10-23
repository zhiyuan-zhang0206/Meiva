# meiva:note
# The pure decisions of the hosted lifecycle: which claimed command may run, what a
# restart leaves for the agent to read, which shell sessions a terminate may take with
# it, and what closing a turn must do. `hosted.py` runs the transactions and executes
# these values.

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from base.agents.messages.kwargs import NoteTag
from base.agents.messages.lifecycle_text import restart_resume_note

LifecycleKind = Literal["restart", "terminate"]


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
def resume_note(source: str, *, resume_turn: bool) -> ResumeNote | None:
    if not resume_turn:
        return None
    return ResumeNote(
        content=restart_resume_note(source),
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
