# meiva:note
# `meiva jobs`: read-only views of the OS jobs this repository generates.

from __future__ import annotations

import argparse

__all__ = ["add_jobs_parser"]


def _h_jobs_heartbeats(args: argparse.Namespace) -> int:
    from cli.commands.jobs.heartbeats import cmd_jobs_heartbeats
    from cli.database.runtime import runtime_of

    return cmd_jobs_heartbeats(runtime=runtime_of(args))


def add_jobs_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    jobs_p = sub.add_parser("jobs", help="inspect the OS jobs this repository generates")
    jobs_sub = jobs_p.add_subparsers(dest="jobs_cmd", required=True)
    heartbeats_p = jobs_sub.add_parser(
        "heartbeats",
        help="print this machine's declared heartbeat set as JSON",
        description=(
            "Every generated OS job declares the heartbeat file it writes when it finishes, the "
            "owner to call when that file stops moving, and the grace it is given. This prints "
            "the whole declared population of this machine, with the jobs its configuration "
            "expects to be installed, so a reader elsewhere can diff it against the scheduler. "
            "Non-zero exit means the declarations could not be read, never an empty set."
        ),
    )
    heartbeats_p.set_defaults(func=_h_jobs_heartbeats)
