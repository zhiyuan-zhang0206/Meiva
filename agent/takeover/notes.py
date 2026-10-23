from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime

INTRODUCTION_ID = "impersonation-introduction"

_EXPLANATION = (
    "Impersonation means a trusted external executor temporarily takes over your "
    "work. You are paused while it acts under your Meiva agent identity. It receives "
    "incoming messages, can use your tools, and can send replies to the user as "
    "you; those replies appear to the user as coming from you, an Meiva agent. "
    "When the takeover ends, you resume with a summary of its work when supplied "
    "and a path to the session record. Review the handoff and continue any "
    "unfinished requests."
)


@dataclass(frozen=True)
class NoteStamp:
    created_at: datetime
    prefix: str


@dataclass(frozen=True)
class SessionRef:
    agent_id: int
    session_id: int
    name: str

    @classmethod
    def of(cls, lease: Mapping[str, str | int]) -> SessionRef:
        agent_id, session_id, name = lease["agent_id"], lease["session_id"], lease["name"]
        if not (
            isinstance(agent_id, int) and isinstance(session_id, int) and isinstance(name, str)
        ):
            raise TypeError(
                "a lease names its agent and session by integer and its executor by text"
            )
        return cls(agent_id=agent_id, session_id=session_id, name=name)


@dataclass(frozen=True)
class NoteDraft:
    note_id: str
    text: str
    created_at: datetime
    anchors_session: bool = False


def introduction_draft(stamp: NoteStamp) -> NoteDraft:
    return NoteDraft(INTRODUCTION_ID, stamp.prefix + _EXPLANATION, stamp.created_at)


def _start_draft(ref: SessionRef, stamp: NoteStamp) -> NoteDraft:
    text = (
        f"Impersonation session {ref.session_id} has started. "
        f'An external executor named "{ref.name}" is taking over your work. '
        "You are paused until it returns control."
    )
    return NoteDraft(
        f"impersonation-start:{ref.agent_id}:{ref.session_id}",
        stamp.prefix + text,
        stamp.created_at,
        anchors_session=True,
    )


# meiva:note
# The notes a takeover's start commits to the checkpoint, in timeline order: the
# introduction of borrowed identity (only once per conversation) and the session's start
# marker. A note whose id the checkpoint already holds is not written again, so a crash
# between accepting the request and saving its anchor is repaired without repeating
# either note.
# meiva:link agent/docs/impersonation/impersonation-handoff.meiva.okf.md
def start_drafts(
    ref: SessionRef, stamp: NoteStamp, *, introduced: bool, existing_ids: Collection[str]
) -> tuple[NoteDraft, ...]:
    drafts = [] if introduced else [introduction_draft(stamp)]
    drafts.append(_start_draft(ref, stamp))
    return tuple(draft for draft in drafts if draft.note_id not in existing_ids)


def handoff_receipt(ref: SessionRef) -> str:
    return f"{ref.agent_id}:{ref.session_id}"


def handoff_note_id(receipt: str) -> str:
    return f"impersonation-handoff:{receipt}"


def handoff_summary(summary: str | None, *, status: str, rejection_reason: str | None) -> str:
    if summary is not None:
        return summary
    return (
        f"Session ended with status {status}. "
        f"{rejection_reason or ''} No external completion summary was supplied."
    )


# meiva:note
# The end-of-session note appended to the native checkpoint as the first resumed input.
# `reason` is the death cause of a supervisor-aborted session; the note names it right
# after the session-end sentence.
# meiva:link agent/docs/impersonation/impersonation-handoff.meiva.okf.md
def handoff_draft(
    ref: SessionRef, stamp: NoteStamp, *, summary: str, path: str, reason: str | None
) -> NoteDraft:
    stopped = f" This session was stopped early: {reason}." if reason else ""
    text = (
        f"Impersonation session {ref.session_id} has ended.{stopped} "
        f'You have resumed execution after the takeover by external executor "{ref.name}".\n\n'
        f"External summary:\n{summary}\n\n"
        f"The session record is available at: {path}\n"
        "Reply to the user in ordinary assistant text in this conversation. "
        "Review the summary and incoming requests in the record. Continue any "
        "requests whose completion is not established by the summary or record. "
        "The activity log may be incomplete; missing entries do not establish "
        "that an action never happened."
    )
    return NoteDraft(handoff_note_id(handoff_receipt(ref)), stamp.prefix + text, stamp.created_at)
