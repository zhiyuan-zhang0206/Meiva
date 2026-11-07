#!/usr/bin/env python3
"""Launch, message, inspect, or stop Google Antigravity conversations.

The launch and control logic lives in ``meiva.shell.coding_tools.antigravity``;
this script is its command-line entry.

Usage::

    # Supervised worker mode (creates tasks.md + work.md, sends contract)
    .venv/bin/python spawn_antigravity.py <workspace-dir>

    # Interactive prompt mode (starts conversation with initial prompt, no files)
    .venv/bin/python spawn_antigravity.py <workspace-dir> --prompt '<initial prompt>'

    # Send a follow-up message (e.g., new tasks or impersonation instructions)
    .venv/bin/python spawn_antigravity.py <workspace-dir> --send <CONVERSATION_ID> --message '<text>'

    # Inspect or stop a conversation
    .venv/bin/python spawn_antigravity.py <workspace-dir> --status <CONVERSATION_ID>
    .venv/bin/python spawn_antigravity.py <workspace-dir> --stop <CONVERSATION_ID>
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from base.host.boundary.options import options
from base.lm.harness.antigravity import antigravity_server_from_environment
from meiva.shell.coding_tools import (
    add_capability_arguments,
    add_lineage_arguments,
    announce_capability,
    antigravity,
    require_lineage,
)

_SKILL_DIR = Path(__file__).resolve().parent.parent


# meiva:note
# The flags this entry reads, as real types: `options` checks every field against the annotation
# here, so nothing past `parse_args` carries an `argparse.Namespace` attribute's `Any`. `mode` stays
# `str` — `announce_capability` takes the string argparse already narrowed to its choices.
@dataclass(frozen=True)
class AntigravityOptions:
    workspace: str
    tasks_file: str | None
    work_file: str | None
    prompt: str | None
    message: str | None
    send: str | None
    status: str | None
    stop: str | None
    mode: str
    force_mode: bool


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch, message, inspect, or stop Google Antigravity conversations."
    )
    parser.add_argument(
        "workspace",
        help="Workspace directory for Antigravity.",
    )
    parser.add_argument(
        "--tasks-file",
        default=None,
        help="Task input file, absolute or relative to the workspace (default: tasks.md).",
    )
    parser.add_argument(
        "--work-file",
        default=None,
        help="STATUS and log file, absolute or relative to the workspace (default: work.md).",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Initial prompt text sent instead of the two-file collaboration contract.",
    )
    parser.add_argument(
        "--message",
        default=None,
        help="Message text for --send ('-' reads stdin).",
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--send",
        metavar="CONVERSATION_ID",
        type=antigravity.session_uuid,
        help="Send --message to an existing Antigravity conversation UUID.",
    )
    action.add_argument(
        "--status",
        metavar="CONVERSATION_ID",
        type=antigravity.session_uuid,
        help="Print metadata for an existing Antigravity conversation UUID.",
    )
    action.add_argument(
        "--stop",
        metavar="CONVERSATION_ID",
        type=antigravity.session_uuid,
        help="Force-stop an existing Antigravity conversation tree.",
    )
    add_lineage_arguments(parser)
    add_capability_arguments(parser)
    return parser


def _has_launch_flags(opts: AntigravityOptions) -> bool:
    return bool(
        opts.prompt is not None or opts.tasks_file is not None or opts.work_file is not None
    )


def _validate_args(parser: argparse.ArgumentParser, opts: AntigravityOptions) -> None:
    non_launch = bool(opts.send is not None or opts.status is not None or opts.stop is not None)
    if opts.send is None and opts.message is not None:
        parser.error("--message requires --send")
    if non_launch and _has_launch_flags(opts):
        parser.error("--send/--status/--stop cannot be combined with launch flags")
    if opts.prompt is not None:
        if opts.tasks_file is not None or opts.work_file is not None:
            parser.error("--tasks-file/--work-file cannot be combined with --prompt")
        if not opts.prompt.strip():
            parser.error("--prompt must be non-empty")


# meiva:note
# The text of one `--send`: required with it, and `-` reads standard input. The check sits where the
# text is read, so the value handed to `send` is a `str` and not the flag's optional type.
def _message_text(opts: AntigravityOptions, parser: argparse.ArgumentParser) -> str:
    message = opts.message
    if message is None:
        parser.error("--send requires --message")
    return sys.stdin.read() if message == "-" else message


def main(argv: list[str]) -> int:
    parser = _build_parser()
    parsed = parser.parse_args(argv)
    opts = options(parsed, AntigravityOptions)
    _validate_args(parser, opts)
    antigravity_server = antigravity_server_from_environment(os.environ)

    if opts.send is not None:
        content = _message_text(opts, parser)
        return antigravity.send(opts.send, content, antigravity_server=antigravity_server)
    if opts.status is not None:
        return antigravity.status(opts.status, antigravity_server=antigravity_server)
    if opts.stop is not None:
        return antigravity.stop(opts.stop, antigravity_server=antigravity_server)

    user_home = Path.home()
    workspace = antigravity.resolve_dir(opts.workspace, user_home=user_home)
    tasks_file = None
    work_file = None
    if opts.prompt is None:
        tasks_file = antigravity.resolve_file(
            workspace, opts.tasks_file or "tasks.md", user_home=user_home
        )
        work_file = antigravity.resolve_file(
            workspace, opts.work_file or "work.md", user_home=user_home
        )
    provenance = None
    prompt = None
    if opts.prompt is not None:
        prompt = f"{require_lineage(parser, parsed, now=datetime.now(UTC))}\n\n{opts.prompt}"
    else:
        provenance = require_lineage(parser, parsed, now=datetime.now(UTC))
    refusal = announce_capability("antigravity", mode=opts.mode, force=opts.force_mode)
    if refusal:
        return refusal
    return antigravity.launch(
        workspace,
        tasks_file,
        work_file,
        prompt=prompt,
        provenance=provenance,
        skill_dir=_SKILL_DIR,
        antigravity_server=antigravity_server,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
