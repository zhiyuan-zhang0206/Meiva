from __future__ import annotations

from dataclasses import dataclass

from base.api_contracts.agent_contract import (
    AgentStatus,
    Harness,
    HarnessStatus,
    ProbedHarnessModel,
    ShellSessionKillTiming,
)

__all_for_meiva__ = [
    "CommandInfo",
    "HarnessAttachment",
    "HarnessAvailability",
    "Machine",
    "Neighbor",
    "ShellSessionsKill",
]


@dataclass(frozen=True)
class CommandInfo:
    name: str
    description: str
    instruction_hint: str

    def __str__(self) -> str:
        hint = f" {self.instruction_hint}" if self.instruction_hint else ""
        desc = f"  — {self.description}" if self.description else ""
        return f"/{self.name}{hint}{desc}"


@dataclass(frozen=True)
class HarnessAttachment:
    """The harness registration the agent's home machine held when the call ran.

    A bridge session writes one row when it starts a provider runtime as the agent
    and retires it when the session closes, so a row that is still there names the
    process driving that session. `controller_pid` is that process and `expires_at`
    the instant after which the row is stale (None when it never expires); judge it
    against your own clock, not the agent's machine.

    The read filters on nothing, expiry included: a row whose `expires_at` has passed is
    still reported with that past instant, so None and a lapsed row are different answers.

    Advisory, like `TerminateOutcome.open_tasks`: it refuses nothing, and a
    terminate carries it without changing what the call did."""

    agent_id: int
    controller_pid: int
    expires_at: str | None

    def __str__(self) -> str:
        expires = self.expires_at if self.expires_at is not None else "never"
        return f"controller pid {self.controller_pid}, expires {expires}"


@dataclass(frozen=True)
class HarnessAvailability:
    """One harness's availability on the machine you are running on.

    harness: which harness the row describes.
    status: "available" (installed, signed in, and model options read),
        "unavailable" (not installed, or installed but not signed in), or
        "unknown" (installed, but sign-in or model options cannot be read here).
    reason: one readable sentence explaining the status.
    binary: the executable this machine would run, or None when the harness has no
        CLI of its own or none was found.
    on_path: whether the executable was found through PATH; False means only a
        conventional install directory held it, so a process started with a
        trimmed PATH may not see it.
    models: live model and reasoning-effort options reported by the harness CLI
        when status is "available" (empty for the built-in meiva runtime, which
        uses the shared model catalog).
    """

    harness: Harness
    status: HarnessStatus
    reason: str
    binary: str | None
    on_path: bool
    models: tuple[ProbedHarnessModel, ...] = ()

    def __str__(self) -> str:
        location = self.binary if self.binary is not None else "built in"
        return f"{self.harness.value}  [{self.status}]  {location}  {self.reason}"


@dataclass(frozen=True)
class Machine:
    name: str
    description: str | None
    live: bool

    def __str__(self) -> str:
        state = "live" if self.live else "offline"
        desc = f"  {self.description}" if self.description else ""
        return f"{self.name}  [{state}]{desc}"


@dataclass(frozen=True)
class Neighbor:
    """`depth`: hops from the queried agent (1 = direct) — out along ties for
    neighbors, up the spawn chain for ancestors; `score`: tie strength.
    Terminated agents are included."""

    agent_id: int
    label: str | None
    status: AgentStatus
    depth: int
    score: float

    def __str__(self) -> str:
        label_part = f"{self.label} " if self.label else ""
        return (
            f"#{self.agent_id} {label_part} {self.status.value}  "
            f"depth={self.depth}  score={self.score:.4g}"
        )


@dataclass(frozen=True)
class ShellSessionsKill:
    """What `kill_all_shell_sessions=True` did. `when` is "now" when the shell
    sessions are already gone — `killed` lists their ids, empty when the agent
    had none — or "at_exit" when they are killed right before the agent stops,
    after its last step (`killed` stays empty)."""

    when: ShellSessionKillTiming
    killed: list[int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "when", ShellSessionKillTiming(self.when))
