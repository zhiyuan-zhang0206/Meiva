# meiva:note
# What a takeover attachment decides, apart from reading the process or the lease row. Every
# function takes the values its shell read once (the public arguments, the lease row's fields, the
# held version, the receipt in the native checkpoint, the pid and a fresh suffix) and returns the
# answer or raises, so the same lease facts always give the same decision and a test states them
# directly. The shell in `attachment.py` reads the lease again on every SDK call and calls these
# with what it read.
# meiva:link meiva/impersonation/docs/attachment-contracts.meiva.okf.md

from __future__ import annotations

from dataclasses import dataclass


# meiva:note
# Which lease an attach call names: the lease handle itself, or an agent's integer session whose
# handle the shell resolves against the database (`lease_id` is empty then).
@dataclass(frozen=True, slots=True)
class LeaseTarget:
    lease_id: str
    agent_id: int | None
    session_id: int | None


def lease_target(session_id: int | str, agent_id: int | None) -> LeaseTarget:
    if isinstance(session_id, int) and not isinstance(session_id, bool) and agent_id is not None:
        return LeaseTarget("", agent_id, session_id)
    if isinstance(session_id, str) and agent_id is None:
        return LeaseTarget(session_id, None, None)
    raise ValueError("attach requires an agent_id and an integer session_id")


def require_held_version(lease_version: int, held_version: int) -> None:
    if lease_version != held_version:
        raise RuntimeError("another attachment changed plugin state; attach again before acting")


# meiva:note
# How many journal entries the native graph already applied: the lease's own counter, or the
# receipt the checkpoint carries when that receipt names this lease and is further along (a crash
# between the checkpoint write and the database acknowledgment leaves the lease behind).
def applied_version(
    lease_applied: int, receipt_lease_id: str | None, receipt_version: int, lease_id: str
) -> int:
    return max(lease_applied, receipt_version if receipt_lease_id == lease_id else 0)


# meiva:note
# The receipt key a log-native lease's event participant opens under, or None when the lease
# predates the event log and records nothing locally.
def participant_source_key(*, log_native: bool, pid: int, suffix: str) -> str | None:
    return f"attachment:{pid}:{suffix}" if log_native else None
