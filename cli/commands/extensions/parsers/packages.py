"""`meiva packages` — the per-machine package refresh surface.

Builders plus their `_h_*` handlers. Handlers lazy-import their `cmd_*`
implementation from ``cli.commands`` so parser building never loads Settings
(see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Literal

from base.host.boundary.options import options


@dataclass(frozen=True)
class _StatusOptions:
    json_output: bool


@dataclass(frozen=True)
class _RefreshOptions:
    check: bool
    package: str | None
    json_output: bool
    from_job: bool


@dataclass(frozen=True)
class _RollbackOptions:
    name: str


@dataclass(frozen=True)
class _PolicyOptions:
    name: str
    update_mode: Literal["auto", "notify", "off"] | None
    check_every: str | None


def _h_packages_status(args: argparse.Namespace) -> int:
    from cli.commands.extensions.packages import cmd_packages_status
    from cli.database.runtime import delivered_config, runtime_of

    runtime = runtime_of(args)
    return cmd_packages_status(
        config=delivered_config(runtime.config),
        home=runtime.home,
        user_home=runtime.session.user_home,
        now=runtime.clock.now(),
        json_output=options(args, _StatusOptions).json_output,
    )


def _h_packages_refresh(args: argparse.Namespace) -> int:
    from cli.commands.extensions.packages import cmd_packages_refresh
    from cli.database.runtime import delivered_config, runtime_of

    opts = options(args, _RefreshOptions)
    runtime = runtime_of(args)
    return cmd_packages_refresh(
        config=delivered_config(runtime.config),
        # meiva:note
        # The refresh reads git through the host, and the host it needs is the machine this
        # invocation runs on with the configuration's own environment.
        host=runtime.host_after_config(),
        repo=runtime.checkout,
        home=runtime.home,
        user_home=runtime.session.user_home,
        now=runtime.clock.now(),
        check_only=opts.check,
        only=opts.package,
        json_output=opts.json_output,
        from_job=opts.from_job,
    )


def _h_packages_rollback(args: argparse.Namespace) -> int:
    from cli.commands.extensions.packages import cmd_packages_rollback
    from cli.database.runtime import runtime_of

    runtime = runtime_of(args)
    return cmd_packages_rollback(
        options(args, _RollbackOptions).name, home=runtime.home, now=runtime.clock.now()
    )


def _duration(value: str) -> str:
    """Argparse type for `--check-every`: validate the duration before any command runs."""
    from cli.commands.duration import parse_duration

    try:
        parse_duration(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def _h_packages_policy(args: argparse.Namespace) -> int:
    from cli.commands.extensions.packages import cmd_packages_policy
    from cli.database.runtime import delivered_config, runtime_of

    opts = options(args, _PolicyOptions)
    if opts.update_mode is None and opts.check_every is None:
        print(
            "meiva: pass --update-mode and/or --check-every — a policy needs at least one field",
            file=sys.stderr,
        )
        return 2
    runtime = runtime_of(args)
    return cmd_packages_policy(
        opts.name,
        config=delivered_config(runtime.config),
        update_mode=opts.update_mode,
        check_every=opts.check_every,
        home=runtime.home,
        user_home=runtime.session.user_home,
    )


def add_packages_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    packages_p = sub.add_parser(
        "packages",
        help="inspect per-package update policy, channels, and refresh state (this machine)",
    )
    packages_sub = packages_p.add_subparsers(dest="packages_cmd", required=True)

    status_p = packages_sub.add_parser(
        "status",
        help="host version + per-package channel / policy / applied rev / last result",
    )
    status_p.add_argument(
        "--json", action="store_true", dest="json_output", help="machine-readable output"
    )
    status_p.set_defaults(func=_h_packages_status)

    refresh_p = packages_sub.add_parser(
        "refresh",
        help="check the channels and apply due updates (skills; this machine)",
    )
    refresh_p.add_argument("--check", action="store_true", help="check only — never stage or apply")
    refresh_p.add_argument(
        "--package", metavar="NAME", default=None, help="limit the pass to one package"
    )
    refresh_p.add_argument(
        "--from-job",
        action="store_true",
        dest="from_job",
        help="OS-job invocation: adds the job gates and respects each package's check cadence",
    )
    refresh_p.add_argument(
        "--json", action="store_true", dest="json_output", help="machine-readable output"
    )
    refresh_p.set_defaults(func=_h_packages_refresh)

    rollback_p = packages_sub.add_parser(
        "rollback",
        help="restore a package's previous tree (the .<name>.prev kept by the last apply)",
    )
    rollback_p.add_argument("name")
    rollback_p.set_defaults(func=_h_packages_rollback)

    policy_p = packages_sub.add_parser(
        "policy",
        help="record an explicit update policy (mode / check interval) on one package",
    )
    policy_p.add_argument("name")
    policy_p.add_argument("--update-mode", choices=["auto", "notify", "off"], default=None)
    policy_p.add_argument(
        "--check-every", metavar="DUR", default=None, type=_duration, help="e.g. 30m / 24h / 7d"
    )
    policy_p.set_defaults(func=_h_packages_policy)
