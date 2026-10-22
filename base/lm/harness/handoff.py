from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from base.api_contracts.agent_contract import Harness

# meiva:note
# The startup prompt is a wire format between the host and every external provider:
# the same six sections are rendered for Claude, Codex and Antigravity, and a waking
# agent reads them on its first turn. Changing the dataclass fields or the section
# order means bumping `HANDOFF_VERSION`, and the field-set test fails until you do.
# meiva:link base/lm/harness/docs/harness.meiva.okf.md
HANDOFF_VERSION = 1

DEFAULT_OVERLAPS: tuple[str, ...] = (
    "Use your native tools for files, shell commands, search, and background tasks or monitors.",
    "Reach cluster state through `meiva` rather than connecting to Postgres or Redis directly.",
)

DEFAULT_DISCIPLINE: tuple[str, ...] = (
    "State what you did and the evidence for it (paths, command output, numbers); do not "
    "narrate plans as if they were results.",
    "If something failed or you could not check it, say so plainly instead of rounding it up.",
    "The host records your final reply to the user; use `meiva.agents` when you need to "
    "message another agent.",
)


@dataclass(frozen=True, slots=True)
class EntryPoint:
    name: str
    purpose: str


@dataclass(frozen=True, slots=True)
class HarnessContribution:
    harnesses: frozenset[Harness]
    entries: tuple[EntryPoint, ...] = ()
    overlaps: tuple[str, ...] = ()
    discipline: tuple[str, ...] = ()


DEFAULT_ENTRIES: tuple[EntryPoint, ...] = (
    EntryPoint(
        "meiva",
        "the cluster SDK for this session; `meiva.attach()` binds your identity, done for "
        "you on first use — start at meiva.help()",
    ),
    EntryPoint("meiva.agents", "spawn or message another agent, or read the one that briefed you"),
    EntryPoint(
        "meiva.self",
        "pause your heartbeat while waiting (`pause_heartbeat`), or end your process when your "
        "role is complete (`terminate`)",
    ),
)


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

    def with_contributions(
        self,
        contributions: Iterable[HarnessContribution],
        *,
        harness: Harness,
    ) -> Handoff:
        seen_entries = {entry.name: entry for entry in self.entries}
        overlaps = list(self.overlaps)
        discipline = list(self.discipline)
        for item in contributions:
            if harness not in item.harnesses:
                continue
            for entry in item.entries:
                if entry.name not in seen_entries:
                    seen_entries[entry.name] = entry
            for rule in item.overlaps:
                if rule not in overlaps:
                    overlaps.append(rule)
            for rule in item.discipline:
                if rule not in discipline:
                    discipline.append(rule)
        return Handoff(
            agent_id=self.agent_id,
            instruction=self.instruction,
            machine=self.machine,
            workspace=self.workspace,
            label=self.label,
            cluster=self.cluster,
            entries=tuple(seen_entries.values()),
            overlaps=tuple(overlaps),
            discipline=tuple(discipline),
        )

    def _python_example_lines(self) -> list[str]:
        names = {entry.name for entry in self.entries}
        lines = [
            "python3 -c '",
            "import meiva",
            "",
            "# Inspect available SDK namespaces and function signatures",
            "meiva.help()",
        ]
        if "meiva.agents" in names:
            lines.extend(
                [
                    "",
                    "# List active agents in the cluster or inspect a peer",
                    "meiva.help(meiva.agents)",
                    "print(meiva.agents.list_agents())",
                ]
            )
        if "meiva.tasks" in names:
            lines.extend(
                [
                    "",
                    "# Inspect or update shared cluster tasks",
                    "meiva.help(meiva.tasks)",
                ]
            )
        if "meiva.memory" in names:
            lines.extend(
                [
                    "",
                    "# Read or write shared cluster memory",
                    "meiva.help(meiva.memory)",
                ]
            )
        lines.append("'")
        return lines

    def _lines(self) -> list[str]:
        label = f" ({self.label})" if self.label else ""
        return [
            f"# Role & Operating Context — Agent #{self.agent_id}{label}",
            "",
            "You are an autonomous agent running inside your native harness environment to "
            "carry out software engineering, research, analysis, and operational work for the "
            "user and your team. Handle tasks end-to-end with your native tools while "
            "coordinating with the broader agent fleet when needed.",
            "",
            "## Meiva & Your Cluster Identity",
            "You are connected to **Meiva**, a multi-agent platform that coordinates agents "
            "across a **Meiva Cluster** of machines, persistent workspaces, shared task "
            "queues, and shared memory. Within this cluster, your identity and home directory "
            "are:",
            f"- agent id: {self.agent_id}",
            f"- label: {self.label or 'unassigned'}",
            f"- machine: {self.machine}",
            f"- workspace: {self.workspace}",
            "",
            "## Cluster Machines",
            "The Meiva Cluster registers the following machines and roles:",
            *_bullets([f"{fact.machine} — {fact.role}" for fact in self.cluster]),
            "",
            "## Using the Meiva Python SDK",
            "The `meiva` Python package is pre-configured in your shell environment and "
            "attaches to your agent identity automatically on import. Reach the cluster "
            "through these active SDK surfaces:",
            *_bullets([f"`{entry.name}` — {entry.purpose}" for entry in self.entries]),
            "",
            "Run Python from your shell/command tool (`python3 -c ...`) whenever you need to "
            "inspect SDK contracts or interact with the cluster. For example:",
            "```bash",
            *self._python_example_lines(),
            "```",
            "",
            "## Tool & Environment Boundaries",
            *_bullets(list(self.overlaps)),
            "",
            "## Collaboration & Reporting",
            *_bullets(list(self.discipline)),
            "",
            "## Standing Instructions",
            self.instruction.strip(),
        ]


def _bullets(items: list[str]) -> list[str]:
    if not items:
        return ["- (none)"]
    return [f"- {item}" for item in items]
