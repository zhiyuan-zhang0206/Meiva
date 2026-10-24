# meiva:note
# The closure notice of one busy persistent shell, and the keys that make its delivery
# idempotent, as values.
#
# A notice is about the shell, not about what it ran: its dedup key is (machine, agent, session,
# shell birth), so a re-sent notice for the same shell is one delivery, while the survivors the
# stop observed are part of the message and not of the key.

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["CRASH_REASON", "STOP_REASON", "ClosureNotice", "claim_key", "one_per_key"]

# meiva:note
# Why a unit closed the session, as the owner's notice names it. A stop that keeps shell
# sessions (`--keep-shell-sessions`) closes none and records nothing.
STOP_REASON = "an operator stop (meiva stop)"

# meiva:note
# The same, for sessions a pty-sessions service that ended uncleanly left behind (a crash, a
# forced stop of the service, a reboot): the sweep found them busy.
CRASH_REASON = "the pty-sessions service ending uncleanly (a crash, a forced stop or a reboot)"


# meiva:note
# One closed busy session and the stop that closed it.
#
# `survivors` are known processes observed alive after best-effort closure, as (pid, command
# name). An empty tuple does not prove all descendants gone. They are not part of the dedup key.
@dataclass(frozen=True)
class ClosureNotice:
    machine: str
    agent_id: int
    session_id: int
    name: str
    shell_pid: int
    shell_birth: str
    reason: str
    closed_at: str
    survivors: tuple[tuple[int, str], ...] = ()

    def dedup_key(self) -> str:
        raw = f"{self.machine}|{self.agent_id}|{self.session_id}|{self.shell_birth}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def as_dict(self) -> dict[str, object]:
        record: dict[str, object] = {
            "machine": self.machine,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "name": self.name,
            "shell_pid": self.shell_pid,
            "shell_birth": self.shell_birth,
            "reason": self.reason,
            "closed_at": self.closed_at,
        }
        if self.survivors:
            record["survivors"] = [{"pid": pid, "name": name} for pid, name in self.survivors]
        return record


# meiva:note
# The one spelling of a notice's idempotency key, shared by the claim and the check.
def claim_key(notice: ClosureNotice) -> str:
    return f"closure-notice:{notice.machine}:{notice.dedup_key()}"


# meiva:note
# One notice per dedup key, the first occurrence winning.
#
# The database batch needs each key spelled once: a batch that carried one key twice would claim
# it once and then deliver every copy that finds itself owned, two inbounds for one shell birth.
def one_per_key(notices: Sequence[ClosureNotice]) -> tuple[ClosureNotice, ...]:
    unique: list[ClosureNotice] = []
    seen: set[str] = set()
    for notice in notices:
        if notice.dedup_key() not in seen:
            seen.add(notice.dedup_key())
            unique.append(notice)
    return tuple(unique)
