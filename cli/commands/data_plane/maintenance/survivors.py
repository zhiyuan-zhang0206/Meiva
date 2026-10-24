# meiva:note
# The deadline report of a held stop: who is left, as values.
#
# Reports retain the captured birth even when live facts cannot be read. `StopSurvivor` is one
# process still alive when a stop's deadline expired; `SurvivorInventory` renders the list for the
# operator and as the JSON-safe payload the lifecycle journal stores. The shell
# (`maintenance_stop_report`) reads the live facts and builds these.

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["StopSurvivor", "SurvivorInventory", "SurvivorRole"]

_CMDLINE_SHOWN = 300


# meiva:note
# What a known process is to its session: the shell itself or any other recorded member.
#
# These are the two strings the pty closure's `Survivor.role` carries on the wire
# (`base/sessions/pty/closure.py`, which owns that vocabulary); `SurvivorRole(raw)` refuses any
# other string where a report is built. This copy goes when base exports its own enum.
class SurvivorRole(Enum):
    SHELL = "shell"
    JOB = "job"


# meiva:note
# One process still alive when a stop's deadline expired.
#
# `birth` and `starttime` are the captured identity pair, the same proof the stop path
# revalidates before signalling; the live fields are read at report time and are best-effort, so
# an unreadable process is reported as unreadable rather than dropped.
@dataclass(frozen=True)
class StopSurvivor:
    pid: int
    role: SurvivorRole
    service: str | None
    ppid: int | None
    pgid: int | None
    status: str | None
    birth: float
    starttime: int | None
    cmdline: str | None

    def render(self) -> str:
        parts = [f"pid={self.pid}"]
        if self.ppid is not None:
            parts.append(f"ppid={self.ppid}")
        if self.pgid is not None:
            parts.append(f"pgid={self.pgid}")
        parts.append(f"role={self.role.value}")
        parts.append(f"service={self.service!r}" if self.service else "service=<unattributed>")
        if self.status is not None:
            parts.append(f"status={self.status}")
        parts.append(f"birth={self.birth:.3f}")
        if self.starttime is not None:
            parts.append(f"starttime={self.starttime}")
        if self.cmdline:
            text = self.cmdline
            if len(text) > _CMDLINE_SHOWN:
                text = text[: _CMDLINE_SHOWN - 3] + "..."
            parts.append(f"cmdline={text!r}")
        else:
            parts.append("cmdline=<unreadable>")
        return "    · " + " ".join(parts)

    def payload(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "role": self.role.value,
            "service": self.service,
            "ppid": self.ppid,
            "pgid": self.pgid,
            "status": self.status,
            "birth": self.birth,
            "starttime": self.starttime,
            "cmdline": self.cmdline,
        }


# meiva:note
# A held stop's deadline report: who is left. `killed` says the survivors outlived a SIGKILL.
@dataclass(frozen=True)
class SurvivorInventory:
    survivors: tuple[StopSurvivor, ...]

    def render(self, *, stage: str, killed: bool = False) -> str:
        outcome = "these outlived their SIGKILL" if killed else "nothing was force-killed"
        header = f"remaining process inventory (stage={stage}; {outcome}):"
        if not self.survivors:
            return f"{header} no survivor readable at the deadline"
        lines = [header, *(survivor.render() for survivor in self.survivors)]
        if killed:
            lines.append(
                "  recovery: a process that outlives SIGKILL is usually another user's (a "
                "root `sudo`) or stuck in the kernel; end it as its owner, then retry the command."
            )
        else:
            lines.append(
                "  recovery: retry the command to reconverge recorded groups, or escalate "
                "explicitly with `meiva stop --mode force` if the listed process(es) are expendable."
            )
        return "\n".join(lines)

    def payload(self) -> list[dict[str, object]]:
        return [survivor.payload() for survivor in self.survivors]
