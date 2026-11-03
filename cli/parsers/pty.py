from __future__ import annotations

import argparse
from dataclasses import dataclass

from cli.boundary.options import options


@dataclass(frozen=True)
class _FreezeOptions:
    holder: str
    reason: str


@dataclass(frozen=True)
class _ResumeOptions:
    generation: str


def _h_pty_freeze(args: argparse.Namespace) -> int:
    from cli.commands.agents.pty import cmd_pty_freeze
    from cli.database.runtime import runtime_of

    opts = options(args, _FreezeOptions)
    runtime = runtime_of(args)
    return cmd_pty_freeze(
        holder=opts.holder,
        reason=opts.reason,
        home=runtime.home,
        clock=runtime.clock,
        rng=runtime.rng,
    )


def _h_pty_status(args: argparse.Namespace) -> int:
    from cli.commands.agents.pty import cmd_pty_status
    from cli.database.runtime import runtime_of

    return cmd_pty_status(home=runtime_of(args).home)


def _h_pty_resume(args: argparse.Namespace) -> int:
    from cli.commands.agents.pty import cmd_pty_resume
    from cli.database.runtime import runtime_of

    opts = options(args, _ResumeOptions)
    return cmd_pty_resume(generation=opts.generation, home=runtime_of(args).home)


def add_pty_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    pty_p = sub.add_parser(
        "pty",
        help="[host] freeze, inspect, or resume host-wide allocation of new PTY sessions",
    )
    pty_sub = pty_p.add_subparsers(dest="pty_cmd", required=True)

    freeze_p = pty_sub.add_parser(
        "freeze",
        help="block every new PTY allocation without interrupting existing sessions",
    )
    freeze_p.add_argument("--holder", required=True, help="operator or workflow owning the freeze")
    freeze_p.add_argument("--reason", required=True, help="why new PTY allocation is frozen")
    freeze_p.set_defaults(func=_h_pty_freeze)

    status_p = pty_sub.add_parser("status", help="show the host freeze generation and metadata")
    status_p.set_defaults(func=_h_pty_status)

    resume_p = pty_sub.add_parser(
        "resume", help="resume allocation only when the exact owning generation is supplied"
    )
    resume_p.add_argument("generation", help="generation token printed by `meiva pty freeze`")
    resume_p.set_defaults(func=_h_pty_resume)
