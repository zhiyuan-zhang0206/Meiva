"""Launch external coding tools (Claude Code, Codex) in persistent shell sessions.

Each tool's launcher (``claude``, ``codex``) opens an ``meiva.shell.sessions``
PTY, starts the tool there, and delivers its first message — a collaboration
contract for a supervised worker, or an inline briefing for a takeover that
impersonates the launching agent. The ``meiva-guide.external-agents`` skill's
``spawn_*.py`` scripts are the command-line entries. Not part of the agent-facing
``meiva.shell`` surface.
"""

import argparse
from collections.abc import Callable

from base.sessions.provenance import DispatchProvenance

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


# meiva:note
# A missing --why is a usage error: the reason is the one field the launcher cannot derive, and a
# worker without it cannot judge whether the work fits. Takeover launches get the three-line form
# because they create no task or work file. The builder is a parameter so a test supplies its own
# instead of patching the agent directory.
def require_lineage(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
    *,
    takeover: bool = False,
    build: Callable[..., DispatchProvenance] | None = None,
) -> str:
    from base.sessions import provenance as provenance_block

    from ._common import build_dispatch_provenance

    collect = build or build_dispatch_provenance
    why = (args.why or "").strip()
    if not why:
        parser.error("--why is required: one line saying why this work is delegated")
    if args.for_task is None and (args.task_label is not None or args.priority is not None):
        parser.error("--task-label/--priority require --for-task")
    block = collect(
        for_task=args.for_task,
        why=why,
        task_label=args.task_label,
        priority=args.priority,
    )
    return provenance_block.render_takeover(block) if takeover else provenance_block.render(block)
