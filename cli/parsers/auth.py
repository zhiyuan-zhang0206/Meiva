# meiva:note
# `meiva auth` — the web login password: argparse builder + its `_h_*` handler.
#
# A thin client over the gateway API (`PUT /api/auth/password`): the gateway owns the rules and
# the stored hash. The handler lazy-imports its implementation from ``cli.commands`` so parser
# building never loads Settings (see ``cli.main`` module docstring).

from __future__ import annotations

import argparse
from dataclasses import dataclass

from cli.boundary.options import options


# meiva:note
# `args.password` is bound by `cli.main` before dispatch: the process entry owns the terminal, so
# it prompts or reads standard input and hands the handler the string.
@dataclass(frozen=True)
class _SetPasswordOptions:
    password: str


def _h_auth_set_password(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.auth import cmd_auth_set_password
    from cli.database.runtime import runtime_of

    return cmd_auth_set_password(
        gateway_access_from_boot(runtime_of(args).config),
        options(args, _SetPasswordOptions).password,
    )


def add_auth_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # meiva:note
    # `meiva auth` — the browser login credential. The cluster secret is a bearer, not a login
    # password; this sets the password a browser types instead.
    auth_p = sub.add_parser(
        "auth",
        help="the web login password (set-password); the cluster secret is not a login password",
    )
    auth_sub = auth_p.add_subparsers(dest="auth_cmd", required=True)

    set_p = auth_sub.add_parser(
        "set-password",
        help=(
            "set the password the web UI login asks for (on the gateway home); it is stored "
            "only as a salted hash and ends the sessions the previous password opened"
        ),
    )
    # meiva:note
    # No default source: where a password comes from is the operator's choice, and neither
    # source takes it from argv, where it would sit in the process list and shell history.
    source = set_p.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--prompt", action="store_true", help="type the password twice at a no-echo prompt"
    )
    source.add_argument(
        "--stdin", action="store_true", help="read the password from the first line of stdin"
    )
    set_p.set_defaults(func=_h_auth_set_password)
