from __future__ import annotations

import argparse


def _h_harness_exec(args: argparse.Namespace) -> int:
    from cli.commands.harness import cmd_harness_exec

    return cmd_harness_exec(
        file=args.file,
        handle=args.handle,
        agent_id=args.agent_id,
        session_id=args.session_id,
    )


def add_harness_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    harness_p = sub.add_parser(
        "harness",
        help="[local] run Python inside a provider harness attachment",
    )
    harness_sub = harness_p.add_subparsers(dest="harness_cmd", required=True)

    exec_p = harness_sub.add_parser(
        "exec",
        help="attach the harness identity, then run one Python file in this process",
    )
    exec_p.add_argument(
        "--file",
        default=None,
        help="Python file to run; '-' or omitted reads standard input",
    )
    exec_p.add_argument(
        "--handle",
        default=None,
        help="harness handle; defaults to $MEIVA_HARNESS_HANDLE from the bridge",
    )
    exec_p.add_argument(
        "--agent-id",
        type=int,
        default=None,
        help="expected agent id; refuses a handle issued for another agent",
    )
    exec_p.add_argument(
        "--session-id",
        default=None,
        help="expected provider session id; refuses a handle issued for another session",
    )
    exec_p.set_defaults(func=_h_harness_exec)
