# meiva:note
# Run one command with this home's machine API token in its environment.
#
# Why a wrapper and not a printer (405 ruling, 2026-10-30): a process that boots through this
# home already receives the token (`deliver_unit_authority` -> `consume_unit`), and every pty
# session inherits a superset of the home environment. The one case with no route was a shell
# script that must call the API directly. Printing the token for `$(...)` would move a
# credential that today lives inside a process onto a command line, where `ps`, shell history,
# `set -x` and any redirect can reach it. So this entry never prints the value: it reads the
# capability once, pins the token into the CHILD environment, and execs. The token is not in
# argv and never reaches this script's stdout; the child sees it exactly as it would after a
# normal boot.
from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from base.host.db_authority.model import AuthorityRefusedError
from base.host.db_authority.unit import consume_unit
from base.paths.home import resolve_meiva_home

# meiva:note
# The environment key name, not a credential value.
TOKEN_ENV = "MEIVA_API_TOKEN"  # noqa: S105


# meiva:note
# The child environment: the caller's own, with this home's token pinned. Overwrites rather than
# defaults - the token is read at call time, so a stale value inherited from an older generation
# must not win. The token argument is never echoed anywhere.
def token_env(environ: Mapping[str, str], token: str) -> dict[str, str]:
    child = dict(environ)
    child[TOKEN_ENV] = token
    return child


# meiva:note
# This home's installed unit API token, read fresh on every call - nothing is cached or written.
# A home with no usable capability exits 2 and names the home and the reason, never a credential:
# a caller that proceeded without a token would call the API unauthenticated.
def read_token(home: Path) -> str:
    try:
        return consume_unit(home).api.token
    except (AuthorityRefusedError, ValueError, OSError) as exc:
        print(f"run-with-token: no machine token for {home}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


# meiva:note
# The command after the literal `--`: a flag-looking argument belongs to the child, not to this
# entry, so parsing stops at the separator instead of guessing. Missing the separator is a usage
# error, never an empty command - running nothing must not look like a successful no-op.
def command_from(argv: Sequence[str]) -> list[str]:
    if "--" not in argv:
        return []
    index = list(argv).index("--")
    return list(argv[index + 1 :])


# meiva:note
# Entry: `--check` answers presence only and exits; otherwise exec the caller's command with the
# token in its environment. The exec path prints one line to stderr so a caller's stdout pipe
# stays clean. No shell sits in front of exec on purpose: the child argv arrives as a list and
# must reach exec as that exact list, because a shell would re-split and re-expand an argument
# the caller already quoted (S606 suppression above the exec). The final `return 0` is
# unreachable - execvpe replaces this process - and exists only to satisfy the return type.
def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run-with-token",
        description="Run a command with this home's machine API token in its environment.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="the meiva home to read the capability from (default: the resolved home)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print TOKEN_PRESENT True/False and exit instead of running a command",
    )
    args, _rest = parser.parse_known_args(argv)
    home = args.home if args.home is not None else resolve_meiva_home()

    if args.check:
        try:
            consume_unit(home)
        except (AuthorityRefusedError, ValueError, OSError):
            print("TOKEN_PRESENT False")
            return 1
        print("TOKEN_PRESENT True")
        return 0

    command = command_from(sys.argv[1:] if argv is None else argv)
    if not command:
        print(
            "run-with-token: usage: run_with_token.py [--home PATH] [--check] -- <command...>",
            file=sys.stderr,
        )
        return 2

    token = read_token(home)
    env = token_env(os.environ, token)
    print(f"run-with-token: TOKEN_PRESENT True; exec {command[0]}", file=sys.stderr)
    try:
        os.execvpe(command[0], command, env)  # noqa: S606
    except OSError as exc:
        print(f"run-with-token: cannot exec {command[0]!r}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
