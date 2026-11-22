"""`meiva computer` — computer-use daemon discovery and operator verbs.

Builder plus its `_h_*` handlers; handlers lazy-import the implementations from
``cli.commands`` so parser building never loads Settings (see the ``cli.main``
module docstring).
"""

from __future__ import annotations

import argparse
from contextlib import suppress


def _h_computer_release(args: argparse.Namespace) -> int:
    from cli.commands.agents.computer import cmd_computer_release
    from cli.database.runtime import runtime_of

    host = getattr(args, "host", None)
    return cmd_computer_release(
        home=runtime_of(args).home, host=host if isinstance(host, str) else None
    )


def _h_computer_list(args: argparse.Namespace) -> int:
    from base.cluster.machine import gateway_auth_headers
    from base.cluster.machine.boot import gateway_access_from_boot
    from base.cluster.machines import list_all_with_description
    from cli.commands.agents.computer import cmd_computer_list
    from cli.database import database_factory_of
    from cli.database.runtime import runtime_of
    from services.desktop.remote.cluster_rpc import probe_cluster_computer_status

    runtime = runtime_of(args)
    cluster_rows: list[tuple[str, str | None, str | None]] = []
    headers: dict[str, str] = {}
    local_machine: str | None = None
    local_description: str | None = None
    with suppress(OSError, RuntimeError, ValueError):
        local_machine = runtime.machine.name()
        local_description = runtime.machine.description()
    with suppress(OSError, RuntimeError, ValueError):
        db = database_factory_of(args)()
        cluster_rows = list_all_with_description(db)
    with suppress(OSError, RuntimeError, ValueError):
        access = gateway_access_from_boot(runtime.config)
        headers = gateway_auth_headers(access)

    return cmd_computer_list(
        home=runtime.home,
        platform_system=runtime.host.platform.system,
        local_machine=local_machine,
        local_description=local_description,
        cluster_machines=cluster_rows,
        cluster_status_probe=lambda url: probe_cluster_computer_status(url, headers=headers),
        json_output=bool(getattr(args, "json", False)),
    )


def add_computer_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # `meiva computer` — discovery and operator commands for computer-use targets.
    computer_p = sub.add_parser(
        "computer",
        help="computer-use target discovery and operator commands (list, release)",
    )
    computer_sub = computer_p.add_subparsers(dest="computer_cmd", required=True)

    list_p = computer_sub.add_parser(
        "list",
        help="list available local, cluster (meiva://), and SSH (ssh://) computer-use targets",
    )
    list_p.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of an aligned table",
    )
    list_p.set_defaults(func=_h_computer_list)

    release_p = computer_sub.add_parser(
        "release",
        help="force-release the screen from its current holder "
        "(operator kick; the next FIFO waiter takes over)",
    )
    release_p.add_argument(
        "--host",
        default=None,
        help="optional target URI (e.g. meiva://macmini or ssh://zzyasus); omit for local",
    )
    release_p.set_defaults(func=_h_computer_release)
