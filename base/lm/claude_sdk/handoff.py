from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_OVERLAPS: tuple[str, ...] = (
    "Do not run your own background-task or monitor machinery, and do not try to schedule or "
    "supervise turns yourself: the host owns the turn loop, the queue and cancellation.",
    "Do not use your own edit/write tooling outside the workspace above, and do not edit another "
    "agent's files.",
    "Do not start MCP servers or reach cluster services over the network; the host passes you "
    "everything you are meant to have.",
)

DEFAULT_DISCIPLINE: tuple[str, ...] = (
    "One reply per turn. State what you did and the evidence for it (paths, command output, "
    "numbers); do not narrate plans as if they were results.",
    "If something failed or you could not check it, say so plainly instead of rounding it up.",
    "The host records your final text; do not message other agents or the user directly.",
)

DEFAULT_ENTRIES: tuple[EntryPoint, ...] = ()


@dataclass(frozen=True, slots=True)
class EntryPoint:
    name: str
    purpose: str


@dataclass(frozen=True, slots=True)
class ClusterFact:
    machine: str
    role: str


@dataclass(frozen=True, slots=True)
class Handoff:
    agent_id: int
    instruction: str
    machine: str
    workspace: Path
    label: str | None = None
    cluster: tuple[ClusterFact, ...] = ()
    entries: tuple[EntryPoint, ...] = field(default_factory=lambda: DEFAULT_ENTRIES)
    overlaps: tuple[str, ...] = field(default_factory=lambda: DEFAULT_OVERLAPS)
    discipline: tuple[str, ...] = field(default_factory=lambda: DEFAULT_DISCIPLINE)

    def __post_init__(self) -> None:
        if self.agent_id <= 0:
            raise ValueError(f"agent id must be positive, got {self.agent_id}")
        if not self.instruction.strip():
            raise ValueError("handoff instruction is empty")

    def render(self) -> str:
        return "\n".join(self._lines()).rstrip() + "\n"

    def size_bytes(self) -> int:
        return len(self.render().encode("utf-8"))

    def _lines(self) -> list[str]:
        label = f" ({self.label})" if self.label else ""
        return [
            f"# Handoff — Meiva agent #{self.agent_id}{label}",
            "",
            "## 1. Identity",
            f"- agent id: {self.agent_id}",
            f"- label: {self.label or 'unassigned'}",
            f"- machine: {self.machine}",
            f"- workspace: {self.workspace}",
            "",
            "## 2. Cluster",
            *_bullets([f"{fact.machine} — {fact.role}" for fact in self.cluster]),
            "",
            "## 3. Entry points",
            "`import meiva` is available in your Python tool. Reach the cluster only through these:",
            *_bullets([f"`{entry.name}` — {entry.purpose}" for entry in self.entries]),
            "",
            "## 4. Do not use",
            *_bullets(list(self.overlaps)),
            "",
            "## 5. Reporting",
            *_bullets(list(self.discipline)),
            "",
            "## 6. Task",
            self.instruction.strip(),
        ]


def _bullets(items: list[str]) -> list[str]:
    if not items:
        return ["- (none)"]
    return [f"- {item}" for item in items]
