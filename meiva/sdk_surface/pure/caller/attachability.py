# meiva:note
# Whether a process may borrow an agent identity (a takeover controller, a harness bridge
# child): only an ordinary process may, not the shared host, not a process that already carries
# a borrowed identity, not a native agent loop and not from inside an exec turn. The facts are
# values the attachment shell reads once from the process, so the refusal is testable without
# booting one; `subject` names what is being attached in the message.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BoundIdentity:
    has_lease: bool
    agent_id: int | None
    owns_loop: bool


@dataclass(frozen=True, slots=True)
class ProcessFacts:
    is_host: bool
    in_exec_turn: bool
    bound: BoundIdentity | None


def attach_refusal(facts: ProcessFacts, *, subject: str) -> str | None:
    if facts.is_host:
        return f"a native agent runtime cannot attach {subject}"
    bound = facts.bound
    if bound is not None and bound.has_lease:
        return "this process already has an external attachment"
    if bound is not None and bound.agent_id is not None and bound.owns_loop:
        return f"a native agent runtime cannot attach {subject}"
    if facts.in_exec_turn:
        return f"an exec turn cannot attach {subject}"
    return None


def refuse_unless_attachable(facts: ProcessFacts, *, subject: str) -> None:
    refusal = attach_refusal(facts, subject=subject)
    if refusal is not None:
        raise RuntimeError(refusal)
