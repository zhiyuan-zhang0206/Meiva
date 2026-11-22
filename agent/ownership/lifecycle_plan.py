# meiva:note
# The pure decisions of the hosted lifecycle: which claimed command may run, what a
# restart leaves for the agent to read, which shell sessions a terminate may take with
# it, and what closing a turn must do. `hosted.py` runs the transactions and executes
# these values.

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, LiteralString, NamedTuple

from base.agents.incarnation.restart_transition import RestartTransition
from base.agents.messages.kwargs import NoteTag
from base.agents.messages.lifecycle_text import (
    fast_acknowledgement_note,
    restart_resume_note,
    restart_transition_note,
)
from base.agents.messages.termination_notice import AncestorTerminationNotice
from base.native_process.runtime_incarnation import RuntimeIncarnation

LifecycleKind = Literal["restart", "terminate"]


# meiva:note
# A harness change a committed restart stored: the harness the agent left, the one it now runs
# on, who asked and the message that went with it, which the history note of the switch repeats.
@dataclass(frozen=True)
class HarnessSwitch:
    old: str
    new: str
    source: str
    message: str


# meiva:note
# What a committed lifecycle command leaves to do after the commit. A terminate's
# `shell_cutoff` is the exclusive shell-ID bound for its mandatory shell cleanup;
# a restart leaves it unset. `switch` is set when a restart stored a new harness.
@dataclass(frozen=True)
class AppliedLifecycle:
    kind: LifecycleKind
    agent_id: int
    shell_cutoff: int | None
    switch: HarnessSwitch | None = None
    ancestor_notification: AncestorTerminationNotice | None = None


# meiva:note
# One committed termination; no recovery input is created.
class ReapedCorpse(NamedTuple):
    agent_id: int
    shell_cutoff: int
    ancestor_notification: AncestorTerminationNotice | None = None


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
# What applying a restart command writes beyond releasing the incarnation: the statements that
# store the transition's overlay and harness, and the one note the agent reads on waking. A command
# without a transition keeps the native shape (`resume_note`: only an unfinished turn gets one); a
# command with one always leaves a note, because the agent has to learn that its harness or
# settings changed whether or not a turn was cut short.
@dataclass(frozen=True)
class RestartApply:
    statements: tuple[tuple[LiteralString, tuple[object, ...]], ...]
    note: ResumeNote | None
    switches_harness: bool


_MERGE_OVERLAY: LiteralString = (
    "UPDATE agents_meta SET config_overlay = COALESCE(config_overlay, '{}'::jsonb) || %s::jsonb "
    "WHERE id = %s"
)
_STORE_HARNESS: LiteralString = (
    "UPDATE agents_meta SET harness = %s, harness_configured_at = clock_timestamp() WHERE id = %s"
)
_MARK_HARNESS_MODEL_SELECTION: LiteralString = (
    "UPDATE agents_meta SET birth_config = COALESCE(birth_config, '{}'::jsonb) "
    "|| '{\"harness_model_selection\": 1}'::jsonb WHERE id = %s"
)


def restart_apply(
    agent_id: int,
    *,
    source: str,
    message: str | None,
    resume_turn: bool,
    transition: RestartTransition | None,
    stored_harness: str,
) -> RestartApply:
    if transition is None:
        return RestartApply(
            (),
            resume_note(source, resume_turn=resume_turn, message=message),
            switches_harness=False,
        )
    statements: list[tuple[LiteralString, tuple[object, ...]]] = []
    if transition.config_overlay:
        statements.append((_MERGE_OVERLAY, (json.dumps(dict(transition.config_overlay)), agent_id)))
    switches = transition.target_harness != stored_harness
    if switches:
        statements.append((_STORE_HARNESS, (transition.target_harness, agent_id)))
        if transition.target_harness != "meiva":
            statements.append((_MARK_HARNESS_MODEL_SELECTION, (agent_id,)))
    note_text = restart_transition_note(
        source,
        message,
        from_harness=stored_harness,
        to_harness=transition.target_harness,
    )
    if update_note := fast_acknowledgement_note(transition.config_overlay):
        note_text = f"{note_text}\n\n{update_note}" if note_text else update_note
    note = ResumeNote(
        content=note_text,
        source=source,
        note_tag=NoteTag.LIFECYCLE_RESTART.value,
    )
    return RestartApply(tuple(statements), note, switches_harness=switches)


# meiva:note
# The exclusive shell-ID cutoff for mandatory post-commit termination cleanup.
# `session_index` is the next shell ID read under the agent row lock, so all older
# sessions existed before the durable transition to `terminated`.
def shell_cutoff(session_index: int) -> int:
    return session_index


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
