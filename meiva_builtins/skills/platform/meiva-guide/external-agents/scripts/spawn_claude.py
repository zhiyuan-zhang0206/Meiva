#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from base.host.boundary.options import options
from meiva.shell.coding_tools import (
    add_capability_arguments,
    add_lineage_arguments,
    announce_capability,
    claude,
    require_lineage,
)

_SKILL_DIR = Path(__file__).resolve().parent.parent


# meiva:note
# The flags this entry reads, as real types: `options` checks every field against the annotation
# here, so nothing past `parse_args` carries an `argparse.Namespace` attribute's `Any`. `mode` stays
# `str` — `announce_capability` takes the string argparse already narrowed to its choices.
@dataclass(frozen=True)
class ClaudeOptions:
    workspace: str
    tasks_file: str | None
    work_file: str | None
    prompt: str | None
    ttl_seconds: float
    caller_instance: str | None
    status: bool
    resume: str | None
    cancel_generation: str | None
    mode: str
    force_mode: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pre-trust a workspace and launch Claude Code in a persistent shell session."
    )
    parser.add_argument(
        "workspace",
        help="Workspace directory for Claude Code. Any directory; no layout is assumed.",
    )
    parser.add_argument(
        "--tasks-file",
        default=None,
        help="Where you append tasks for the agent, absolute or relative to the "
        "workspace (default: tasks.md). Supervised worker mode only.",
    )
    parser.add_argument(
        "--work-file",
        default=None,
        help="Where the agent reports STATUS + log; this is what watch_work.py "
        "polls, absolute or relative to the workspace (default: work.md). "
        "Supervised worker mode only.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Initial prompt text sent instead of the two-file collaboration contract.",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=float,
        default=float(claude.DEFAULT_TTL_SECONDS),
        help="Task-adapted hard expiry, up to one day (default: %(default)g).",
    )
    parser.add_argument(
        "--caller-instance",
        default=None,
        help="opt in to v1 external provenance (bounded instance ID); requires target protocol support",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--status",
        action="store_true",
        help="Print the workspace's owner records.",
    )
    action.add_argument(
        "--resume",
        metavar="SESSION_ID",
        type=claude.session_uuid,
        help="Reopen this recorded Claude Code session (the claude_session a launch printed) instead of starting a new one.",
    )
    action.add_argument(
        "--cancel-generation",
        metavar="GENERATION",
        help="Stop and terminalize exactly this generation.",
    )
    add_lineage_arguments(parser)
    add_capability_arguments(parser)
    return parser


def _validate_args(parser: argparse.ArgumentParser, opts: ClaudeOptions) -> None:
    if opts.prompt is not None:
        if opts.status or opts.cancel_generation:
            parser.error("--prompt requires a launch")
        if opts.tasks_file is not None or opts.work_file is not None:
            parser.error("--tasks-file/--work-file cannot be combined with --prompt")
        if not opts.prompt.strip():
            parser.error("--prompt must be non-empty")


def main(argv: list[str]) -> int:
    parser = _build_parser()
    parsed = parser.parse_args(argv)
    opts = options(parsed, ClaudeOptions)
    _validate_args(parser, opts)

    user_home = Path.home()
    workspace = claude.placed_path(opts.workspace, user_home=user_home).resolve()
    if not opts.status and not opts.cancel_generation:
        workspace = claude.resolve_dir(opts.workspace, user_home=user_home)
    if opts.status:
        return claude.status(workspace, mode=opts.mode, force=opts.force_mode)
    if opts.cancel_generation:
        return claude.cancel(workspace, opts.cancel_generation)
    tasks_file = None
    work_file = None
    if opts.prompt is None:
        tasks_file = claude.resolve_file(
            workspace, opts.tasks_file or "tasks.md", user_home=user_home
        )
        work_file = claude.resolve_file(workspace, opts.work_file or "work.md", user_home=user_home)
    provenance = None
    prompt = None
    if opts.prompt is not None:
        prompt = f"{require_lineage(parser, parsed, now=datetime.now(UTC))}\n\n{opts.prompt}"
    elif opts.resume is None:
        provenance = require_lineage(parser, parsed, now=datetime.now(UTC))
    refusal = announce_capability("claude", mode=opts.mode, force=opts.force_mode)
    if refusal:
        return refusal
    return claude.launch(
        workspace,
        tasks_file,
        work_file,
        opts.ttl_seconds,
        opts.caller_instance,
        resume=opts.resume,
        prompt=prompt,
        provenance=provenance,
        skill_dir=_SKILL_DIR,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
