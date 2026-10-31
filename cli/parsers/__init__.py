# meiva:note
# `meiva` CLI argparse surface — per-domain parser builders + their `_h_*` handlers.
#
# `cli.main` imports this package at module level (settings-free: nothing here imports runtime
# command modules / ``base.config``, so ``meiva --help`` builds the tree on a host with no .env)
# and calls :func:`build_parser` from ``main()``. Each builder binds its own module's handler
# directly (``set_defaults(func=_h_x)``); a handler is exercised by parsing an argv and calling the
# bound ``func`` with the namespace the entry would have built. Handlers lazy-import their
# ``cmd_*`` implementation so the Settings load stays deferred to dispatch time — see the
# ``cli.main`` module docstring.
#
# One module per domain:
#
# - ``host`` — init/start/stop/restart/status/converge/firewall
# - ``observability`` — lgtm/trace
# - ``cluster`` — the whole-cluster verbs
# - ``cli.commands.agents.parsers`` — agents + notices, beside their implementations
# - ``cli.commands.agents.impersonation_parsers`` — external sessions and relays
# - ``backup`` — WAL-G physical backups
# - ``cli.commands.extensions.parsers`` — plugins, skill, MCP, memory and packages
# - ``management`` — config + presets + schedules

from __future__ import annotations

import argparse
import subprocess
from typing import TypeGuard

from cli.commands.agents.impersonation_parsers import add_impersonation_parser
from cli.commands.agents.parsers import add_agents_parser
from cli.commands.extensions.parsers.mcp import add_mcp_parser, add_memory_parser
from cli.commands.extensions.parsers.packages import add_packages_parser
from cli.commands.extensions.parsers.plugins import add_plugins_parser, add_skill_parser
from cli.parsers.backup import add_backup_parser
from cli.parsers.cluster import add_cluster_parser
from cli.parsers.computer import add_computer_parser
from cli.parsers.harness import add_harness_parser
from cli.parsers.host import (
    add_converge_parser,
    add_firewall_parser,
    add_init_parser,
    add_restart_parser,
    add_start_parser,
    add_status_parser,
    add_stop_parser,
)
from cli.parsers.logs import add_logs_parser
from cli.parsers.management import (
    add_config_parser,
    add_schedules_parser,
)
from cli.parsers.observability import add_lgtm_parser, add_trace_parser
from cli.parsers.permissions import add_permissions_parser
from cli.parsers.pty import add_pty_parser

__all__ = ["build_parser", "command_options", "parse_args"]


def build_parser(
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> argparse.ArgumentParser:
    # meiva:note
    # Build the full `meiva` argparse tree; each subparser binds ``func=`` here. The registration
    # order below IS the `meiva --help` listing order — keep it in sync with the command surface.
    parser = argparse.ArgumentParser(
        prog="meiva",
        description="Meiva cluster ops: native pg/redis + service sessions + cron healthchecks.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    add_init_parser(sub)
    add_start_parser(sub, retained_children=retained_children)
    add_stop_parser(sub, retained_children=retained_children)
    add_restart_parser(sub, retained_children=retained_children)
    add_status_parser(sub)
    add_pty_parser(sub)
    add_harness_parser(sub)
    add_converge_parser(sub)
    add_firewall_parser(sub)
    add_permissions_parser(sub)
    add_lgtm_parser(sub, retained_children=retained_children)
    add_cluster_parser(sub)
    add_computer_parser(sub)
    add_trace_parser(sub)
    add_logs_parser(sub)
    add_backup_parser(sub)
    add_agents_parser(sub)
    add_impersonation_parser(sub)
    add_config_parser(sub)
    add_schedules_parser(sub)
    add_plugins_parser(sub)
    add_skill_parser(sub)
    add_mcp_parser(sub)
    add_memory_parser(sub)
    add_packages_parser(sub)

    return parser


def parse_args(
    argv: list[str],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> argparse.Namespace:
    # meiva:note
    # Parse the operator command without dispatch or Settings construction. The entry names the
    # arguments it parsed; nothing here reads `sys.argv`. Help and invalid arguments retain
    # argparse's output and SystemExit codes. Lifecycle handlers keep the supplied child-owner list
    # when the caller later dispatches the returned namespace.
    return build_parser(retained_children=retained_children).parse_args(argv)


def _is_subparsers_action(
    action: argparse.Action,
) -> TypeGuard[argparse._SubParsersAction[argparse.ArgumentParser]]:
    return isinstance(action, argparse._SubParsersAction)


def command_options() -> dict[tuple[str, ...], set[str]]:
    # meiva:note
    # Long options accepted at each command path, including the `meiva` root. Aliases have their
    # own paths. Options belong only to the parser accepting them, so a start option never
    # certifies the same spelling on stop. Return a fresh snapshot for documentation checks
    # without loading command runtimes.
    root = build_parser()
    by_path: dict[tuple[str, ...], set[str]] = {}
    pending: list[tuple[tuple[str, ...], argparse.ArgumentParser]] = [(("meiva",), root)]
    while pending:
        path, parser = pending.pop()
        by_path[path] = {
            option
            for action in parser._actions
            for option in action.option_strings
            if option.startswith("--")
        }
        for action in parser._actions:
            if _is_subparsers_action(action):
                pending.extend(((*path, name), child) for name, child in action.choices.items())
    return by_path
