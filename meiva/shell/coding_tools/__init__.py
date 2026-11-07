"""Launch external coding tools (Claude Code, Codex) in persistent shell sessions.

Each tool's launcher (``claude``, ``codex``) opens an ``meiva.shell.sessions``
PTY, starts the tool there, and delivers its first message — a collaboration
contract for a supervised worker, or an inline briefing for a takeover that
impersonates the launching agent. The ``meiva-guide.external-agents`` skill's
``spawn_*.py`` scripts are the command-line entries. Not part of the agent-facing
``meiva.shell`` surface.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime

_PRIORITIES = ("P0", "P1", "P2", "P3")


# meiva:note
# Every spawn script shares these flags so a launch carries the same lineage fields. --why is the
# only field a person writes; the rest is generated from cluster state, so a launch cannot claim a
# parent or a task that is not real.
def add_lineage_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--for-task",
        type=int,
        default=None,
        metavar="ID",
        help="Task this launch serves; the worker reads its id in the provenance block.",
    )
    parser.add_argument(
        "--task-label",
        default=None,
        help="Optional task title for the provenance block; requires --for-task.",
    )
    parser.add_argument(
        "--priority",
        choices=_PRIORITIES,
        default=None,
        help="Optional task priority for the provenance block; requires --for-task.",
    )
    parser.add_argument(
        "--why",
        default=None,
        help="One line saying why this work is delegated; required for a launch.",
    )


def owner_terminated(agent_id: int) -> bool:
    # meiva:note
    # The probe lives in the private launch plumbing (`_common`); this door is how
    # the rest of the tree (and a test standing in for a plugin) reaches it. The
    # import stays inside the call because `_common` imports `meiva.agents`, which
    # must not load while this package is being imported.
    from ._common import owner_terminated as probe

    return probe(agent_id)


# meiva:note
# The flags `add_lineage_arguments` adds, as `require_lineage` reads them from the parsed namespace.
@dataclass(frozen=True)
class _LineageOptions:
    why: str | None
    for_task: int | None
    task_label: str | None
    priority: str | None


# meiva:note
# A missing --why is a usage error: the reason is the one field the launcher cannot derive, and a worker without it cannot judge whether the work fits. The facts of who dispatched the launch are read from the cluster here, once; the block itself is a pure function of them.
def require_lineage(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    now: datetime,
) -> str:
    from base.host.boundary.options import options
    from base.sessions import provenance as provenance_block

    from ._common import read_dispatch_facts
    from .plans.lineage import dispatch_provenance, lineage_error

    flags = options(args, _LineageOptions)
    why = (flags.why or "").strip()
    for_task = flags.for_task
    task_label = flags.task_label
    priority = flags.priority
    error = lineage_error(why, for_task, task_label, priority)
    if error is not None:
        parser.error(error)
    block = dispatch_provenance(
        read_dispatch_facts(),
        for_task=for_task,
        why=why,
        task_label=task_label,
        priority=priority,
        now=now,
    )
    return provenance_block.render(block)


# meiva:note
# Visibility before behaviour: the flags exist so an operator can see which path a launch would take
# and refuse a path this build does not implement, while the launcher still only has one path. An
# explicit --mode names a decision the operator made; --force-mode lets that decision through when
# only the recorded version floor is against it.
def add_capability_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--mode",
        choices=("auto", "pty", "stream-json"),
        default="auto",
        help="Execution path for this launch; auto picks what this build implements.",
    )
    parser.add_argument(
        "--force-mode",
        action="store_true",
        help="Use the requested path even when the detected version is below the recorded floor.",
    )


def capability_report(tool: str, *, mode: str = "auto", force: bool = False) -> tuple[str, int]:
    from ._common import capability_report as report

    return report(tool, mode=mode, force=force)


# meiva:note
# The report is printed before any process starts, so a refusal is visible in the operator's terminal
# rather than as a launch that quietly took the other path.
def announce_capability(tool: str, *, mode: str, force: bool) -> int:
    line, code = capability_report(tool, mode=mode, force=force)
    if line:
        print(line)
    return code
