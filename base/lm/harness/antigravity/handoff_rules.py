from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

from base.lm.harness.handoff import (
    DEFAULT_DISCIPLINE,
    DEFAULT_OVERLAPS,
    ClusterFact,
    EntryPoint,
    Handoff,
)

# meiva:note
# Two properties of `agy`'s tool runtime cannot be fixed by the adapter, only
# disclosed to the agent: every `run_command` is a fresh shell, and the process
# group is killed when the turn ends. Leaving them implicit produces work that
# "succeeds" while the second command runs in the wrong directory, or a resident
# bootstrap that dies with the turn. The text lives here, next to the code that
# cannot change the behaviour.
# meiva:link base/lm/harness/antigravity/docs/antigravity.meiva.okf.md
SHELL_SEMANTICS: tuple[str, ...] = (
    "Every `run_command` call starts a fresh shell: `cd` and `export` do not carry over "
    "to the next call. Put the working directory and every variable in the same command "
    "line, or use absolute paths.",
    "A background process started with `&` is killed when your turn ends. Start anything "
    "that must outlive the turn with `setsid nohup … &` or inside tmux.",
)


# meiva:note
# A `Handoff` that also carries the `agy` shell semantics.
def build_handoff(
    *,
    agent_id: int,
    instruction: str,
    machine: str,
    workspace: Path,
    label: str | None = None,
    cluster: Iterable[ClusterFact] = (),
    entries: Iterable[EntryPoint] = (),
    overlaps: Sequence[str] = DEFAULT_OVERLAPS,
    discipline: Sequence[str] = DEFAULT_DISCIPLINE,
) -> Handoff:
    return Handoff(
        agent_id=agent_id,
        instruction=instruction,
        machine=machine,
        workspace=workspace,
        label=label,
        cluster=tuple(cluster),
        entries=tuple(entries),
        overlaps=(*overlaps, *SHELL_SEMANTICS),
        discipline=tuple(discipline),
    )
