# meiva:note
# Pure eligibility checks, CAS comparison, and passive `system_note` composition for whole-agent
# cross-machine transfers.
#
# No database, filesystem, network, clock, or randomness access lives here (`purity = "strict"`).
# meiva:link gateway/agents/docs/agent-transfer.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID


@dataclass(frozen=True)
class AgentRowFacts:
    agent_id: int
    machine: str
    status: str
    harness: str
    runtime_owner: UUID | None
    runtime_generation: UUID | None
    last_admission_at: datetime | None
    session_index: int


@dataclass(frozen=True)
class PendingLifecycleFact:
    kind: str
    status: str


@dataclass(frozen=True)
class ImpersonationConflictFact:
    session_id: int
    status: str
    delta_unapplied: bool
    handoff_unapplied: bool


@dataclass(frozen=True)
class PendingUploadFact:
    batch_id: str
    state: str


@dataclass(frozen=True)
class MachineFacts:
    name: str
    gateway_url: str | None
    roles: tuple[str, ...]
    paused: bool
    stopped: bool


@dataclass(frozen=True)
class AgentTransferSnapshot:
    agent_id: int
    from_machine: str
    status: Literal["idling", "terminated"]
    runtime_owner: UUID | None
    runtime_generation: UUID | None
    last_admission_at: datetime | None
    session_index: int


@dataclass(frozen=True)
class TransferRefusal:
    status_code: int
    detail: str


@dataclass(frozen=True)
class TransferPlan:
    snapshot: AgentTransferSnapshot
    source_ops_url: str
    to_machine: str


@dataclass(frozen=True)
class TransferPrecheckFacts:
    agent: AgentRowFacts | None
    agent_id: int
    to_machine: str
    pending_lifecycle: PendingLifecycleFact | None = None
    impersonation: ImpersonationConflictFact | None = None
    pending_upload: PendingUploadFact | None = None
    source_machine: MachineFacts | None = None
    target_machine: MachineFacts | None = None


def _parse_settled_status(status: str) -> Literal["idling", "terminated"]:
    if status == "idling":
        return "idling"
    if status == "terminated":
        return "terminated"
    raise ValueError(f"unsupported status {status!r} for transfer")


def _check_agent_state(agent: AgentRowFacts) -> Literal["idling", "terminated"] | TransferRefusal:
    if agent.harness != "meiva":
        return TransferRefusal(
            409,
            f"agent {agent.agent_id} runs external harness {agent.harness!r}; "
            "only native 'meiva' agents can be transferred across machines",
        )
    if agent.status == "running":
        return TransferRefusal(
            409,
            f"agent {agent.agent_id} is currently running on {agent.machine!r}; "
            "terminate it or wait for its turn to settle before transferring",
        )
    try:
        return _parse_settled_status(agent.status)
    except ValueError:
        return TransferRefusal(
            409,
            f"agent {agent.agent_id} has unsupported status {agent.status!r} for transfer",
        )


def _check_blocking_activity(facts: TransferPrecheckFacts) -> TransferRefusal | None:
    agent_id = facts.agent_id
    if facts.pending_lifecycle is not None:
        return TransferRefusal(
            409,
            f"agent {agent_id} has an in-flight {facts.pending_lifecycle.kind!r} command "
            f"({facts.pending_lifecycle.status}); wait for it to settle before transferring",
        )
    if facts.impersonation is not None:
        imp = facts.impersonation
        if imp.status in ("requested", "accepted", "active"):
            return TransferRefusal(
                409,
                f"agent {agent_id} has an open impersonation session "
                f"(session {imp.session_id}, status={imp.status!r}); "
                "release or force-expire it before transferring",
            )
        return TransferRefusal(
            409,
            f"agent {agent_id} has an unsettled impersonation handoff "
            f"(session {imp.session_id}, status={imp.status!r}); "
            "let its home runner settle the handoff before transferring",
        )
    if facts.pending_upload is not None:
        return TransferRefusal(
            409,
            f"agent {agent_id} has an in-flight upload delivery batch "
            f"({facts.pending_upload.batch_id}, state={facts.pending_upload.state!r}); "
            "wait for delivery to complete before transferring",
        )
    return None


def _check_machines(agent: AgentRowFacts, facts: TransferPrecheckFacts) -> str | TransferRefusal:
    to_machine = facts.to_machine
    if to_machine == agent.machine:
        return TransferRefusal(409, f"agent {agent.agent_id} is already on machine {to_machine!r}")
    target = facts.target_machine
    if target is None:
        return TransferRefusal(
            404, f"target machine {to_machine!r} is not registered in the machines table"
        )
    if "agent-runner" not in target.roles:
        return TransferRefusal(
            409, f"target machine {to_machine!r} does not advertise the 'agent-runner' role"
        )
    if target.paused:
        return TransferRefusal(
            409,
            f"target machine {to_machine!r} is paused; resume it before transferring agents to it",
        )
    if target.stopped:
        return TransferRefusal(
            409,
            f"target machine {to_machine!r} is marked stopped; start it before transferring agents to it",
        )
    if not target.gateway_url:
        return TransferRefusal(503, f"target machine {to_machine!r} has no reachable ops URL")
    source = facts.source_machine
    if source is None or not source.gateway_url:
        return TransferRefusal(
            503,
            f"source machine {agent.machine!r} has no reachable ops URL in the machines table",
        )
    if source.stopped:
        return TransferRefusal(
            503,
            f"source machine {agent.machine!r} is marked stopped; "
            "start it so its workspace can be exported",
        )
    return source.gateway_url


# meiva:note
# Decide whether `facts.agent` may be transferred to `facts.to_machine`, returning either a concrete
# HTTP refusal (`404`, `409`, `503`) or the immutable `TransferPlan` that fences workspace export
# and final Postgres cutover.
def evaluate_precheck(facts: TransferPrecheckFacts) -> TransferPlan | TransferRefusal:
    if facts.agent is None:
        return TransferRefusal(404, f"agent {facts.agent_id} does not exist")
    settled = _check_agent_state(facts.agent)
    if isinstance(settled, TransferRefusal):
        return settled
    blocked = _check_blocking_activity(facts)
    if blocked is not None:
        return blocked
    source_ops_url = _check_machines(facts.agent, facts)
    if isinstance(source_ops_url, TransferRefusal):
        return source_ops_url
    agent = facts.agent
    return TransferPlan(
        snapshot=AgentTransferSnapshot(
            agent_id=agent.agent_id,
            from_machine=agent.machine,
            status=settled,
            runtime_owner=agent.runtime_owner,
            runtime_generation=agent.runtime_generation,
            last_admission_at=agent.last_admission_at,
            session_index=agent.session_index,
        ),
        source_ops_url=source_ops_url,
        to_machine=facts.to_machine,
    )


# meiva:note
# Verify under `SELECT ... FOR UPDATE` that the agent's placement, status, runtime ownership,
# admission timestamp, and shell sequence number have not moved while workspace bytes were copied.
def verify_cutover_cas(
    snapshot: AgentTransferSnapshot,
    current: AgentRowFacts | None,
) -> str | None:
    if current is None:
        return f"agent {snapshot.agent_id} disappeared before cutover"
    if current.harness != "meiva":
        return f"agent {snapshot.agent_id} harness is {current.harness!r}, expected 'meiva'"
    if current.machine != snapshot.from_machine:
        return (
            f"agent {snapshot.agent_id} moved from {snapshot.from_machine!r} "
            f"to {current.machine!r} during transfer"
        )
    if current.status != snapshot.status:
        return (
            f"agent {snapshot.agent_id} status changed from {snapshot.status!r} "
            f"to {current.status!r} during transfer"
        )
    if (
        current.runtime_owner != snapshot.runtime_owner
        or current.runtime_generation != snapshot.runtime_generation
        or current.last_admission_at != snapshot.last_admission_at
    ):
        return f"agent {snapshot.agent_id} admitted a new turn during transfer"
    if current.session_index != snapshot.session_index:
        return f"agent {snapshot.agent_id} allocated a new shell session during transfer"
    return None


# meiva:note
# Compose the passive `system_note` (`NoteTag.CONTEXT`, `passive=True`) recorded on cutover so the
# agent learns on its next turn on `to_machine` that its host changed and machine-local shells/pages
# on `from_machine` were closed.
def compose_transfer_system_note(
    *,
    from_machine: str,
    to_machine: str,
    source: str,
    message: str | None,
    killed_shell_sessions: list[int],
    closed_pages: list[str],
) -> str:
    details: list[str] = [
        f"You were transferred from machine '{from_machine}' to '{to_machine}' by {source}.",
        f"Your workspace files are now at your workspace directory on '{to_machine}'.",
    ]
    if killed_shell_sessions:
        joined_shells = ", ".join(str(sid) for sid in killed_shell_sessions)
        details.append(
            f"Persistent shell session(s) on '{from_machine}' were closed ({joined_shells})."
        )
    if closed_pages:
        joined_pages = ", ".join(closed_pages)
        details.append(f"Open UI page(s) on '{from_machine}' were closed ({joined_pages}).")
    if message:
        details.append(f"Reason: {message}")
    return " ".join(details)
