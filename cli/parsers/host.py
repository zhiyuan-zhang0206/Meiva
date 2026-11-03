# meiva:note
# `meiva` host-level lifecycle verbs — argparse builders + their `_h_*` handlers.
#
# `init` / `start` / `stop` / `restart` / `status` / `converge` / `firewall` act
# on THIS host (or the unit this checkout owns), as opposed to the cluster-wide
# verbs in ``cli.parsers.cluster``. Handlers stay thin: each lazy-imports its
# `cmd_*` implementation from ``cli.commands`` so building the parser never loads
# Settings (see ``cli.main`` module docstring).

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from typing import Literal

from cli.options import attribute, options, raw

# meiva:note
# A lifecycle verb defaults and infers nothing: every choice is the operator's
# explicit answer, and a missing answer is a usage error naming every missing
# choice at once (docs/conventions/engineering/cli-argument-discipline.md).
USAGE_EXIT = 2


def h_init(args: argparse.Namespace) -> int:
    from cli.init_intent import run_init
    from cli.start_runtime import site_of
    from cli.startup.service_path import CallerHost

    return run_init(args, site_of(args), attribute(args, "caller_host", CallerHost))


def _h_start(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    from cli.database import database_factory_of, producer_of
    from cli.start_intent import run_start
    from cli.start_runtime import site_of

    return run_start(
        args,
        site_of(args),
        retained_children=retained_children,
        database_factory=database_factory_of(args),
        producer=producer_of(args),
    )


def add_choice(
    parser: argparse.ArgumentParser | argparse._ArgumentGroup,
    dest: str,
    flags: tuple[str, str],
    *,
    helps: tuple[str, str],
) -> None:
    # meiva:note
    # One required keep-or-stop choice: `flags[0]` answers True, `flags[1]` False.
    #
    # Unanswered reads None; `missing_choices` turns that into the usage error.
    group = parser.add_mutually_exclusive_group()
    for flag, value, text in zip(flags, (True, False), helps, strict=True):
        group.add_argument(
            flag, dest=dest, action="store_const", const=value, default=None, help=text
        )


def missing_choices(args: argparse.Namespace, choices: Sequence[tuple[str, str]]) -> list[str]:
    # meiva:note
    # The answer forms of every choice in `(dest, answer form)` left unanswered.
    return [form for dest, form in choices if raw(args, dest) is None]


def refuse_conflict(verb: str, error: ValueError) -> int:
    # meiva:note
    # A contradictory answer is the operator's answer, not a crash: one line naming
    # the conflict and both ways out, under the exit status the exception already had.
    print(f"meiva {verb}: {error}", file=sys.stderr)
    return 1


def refuse_usage(verb: str, problems: Sequence[str]) -> int:
    # meiva:note
    # Name every missing or inapplicable choice at once, then exit as a usage error.
    print(
        f"meiva {verb}: every lifecycle choice is explicit; fix all of:\n"
        + "\n".join(f"  {problem}" for problem in problems),
        file=sys.stderr,
    )
    return USAGE_EXIT


_STOP_CHOICES = (
    ("keep_shell_sessions", "--keep-shell-sessions | --close-shell-sessions"),
    ("keep_browser", "--keep-browser | --stop-browser"),
    ("keep_extras", "--keep-extras | --stop-extras"),
    ("keep_service", "--keep-service NAME [--keep-service NAME ...] | --keep-no-services"),
    ("mode", "--mode smooth|force"),
    ("timeout", "--timeout SECONDS"),
    ("message", "--message TEXT | --no-message"),
    ("assume_yes", "--yes | --prompt"),
)


def stop_problems(args: argparse.Namespace, roles: frozenset[str] | None) -> list[str]:
    # meiva:note
    # Every missing or inapplicable `meiva stop` choice. The host's capabilities
    # (`roles`, None when unreadable) only refuse: a gateway requires the data-plane
    # choice, any other host accepts none, unreadable capabilities refuse the stop.
    problems = missing_choices(args, _STOP_CHOICES)
    if roles is None:
        problems.append(
            "this home's capabilities are unreadable, so --keep-infra/--stop-infra cannot be "
            "validated; finish `meiva init` first"
        )
    elif "gateway" in roles and raw(args, "keep_infra") is None:
        problems.append("--keep-infra | --stop-infra (this host serves the gateway)")
    elif "gateway" not in roles and raw(args, "keep_infra") is not None:
        problems.append("--keep-infra/--stop-infra: this host has no local data plane")
    return problems


@dataclass(frozen=True)
class _StopAnswers:
    # meiva:note
    # What a `meiva stop` that passed `stop_problems` carries: every choice but `keep_infra`
    # (a gateway host's alone) is answered, so none of these is None.
    keep_infra: bool | None
    keep_service: Sequence[str]
    keep_browser: bool
    keep_shell_sessions: bool
    keep_extras: bool
    mode: Literal["smooth", "force"]
    message: str
    assume_yes: bool
    timeout: float
    maintenance_for: str | None
    reason: str | None


def h_stop(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    from base.cluster.machine import MachineRoleInvalid, MachineRoleMissing
    from cli.database.runtime import runtime_of

    runtime = runtime_of(args)
    try:
        roles: frozenset[str] | None = runtime.machine.role()
    except (MachineRoleMissing, MachineRoleInvalid):
        roles = None
    if problems := stop_problems(args, roles):
        return refuse_usage("stop", problems)
    from cli.commands.duration import parse_duration
    from cli.commands.lifecycle.stop import cmd_stop
    from cli.commands.lifecycle.stop.choices import StopChoices, StopRequest, StopWindow
    from cli.commands.lifecycle.stop.execute import StopEnvironment
    from cli.database import database_factory_of, producer_of

    answers = options(args, _StopAnswers)
    try:
        maintenance_for_seconds = (
            parse_duration(answers.maintenance_for) if answers.maintenance_for else None
        )
    except ValueError as error:
        return refuse_usage("stop", [str(error)])

    request = StopRequest(
        choices=StopChoices(
            keep_infra=answers.keep_infra,
            preserve_sessions=frozenset(answers.keep_service),
            keep_browser=answers.keep_browser,
            keep_shell_sessions=answers.keep_shell_sessions,
            announce=True,
            teardown_extras=not answers.keep_extras,
            force=answers.mode == "force",
            message=answers.message or None,
        ),
        window=StopWindow(for_seconds=maintenance_for_seconds, reason=answers.reason),
        require_confirmation=not answers.assume_yes,
        timeout=answers.timeout,
    )
    environment = StopEnvironment(
        command=runtime,
        database_factory=database_factory_of(args),
        producer=producer_of(args),
        stdin=sys.stdin,
    )
    try:
        return cmd_stop(request, environment, retained_children)
    except ValueError as error:
        return refuse_conflict("stop", error)


_RESTART_TREE_CHOICES = (
    ("keep_shell_sessions", "--keep-shell-sessions | --close-shell-sessions"),
    ("keep_browser", "--keep-browser | --restart-browser"),
    ("keep_extras", "--keep-extras | --restart-extras"),
)
_RESTART_COMMON_CHOICES = (
    ("mode", "--mode smooth|force"),
    ("timeout", "--timeout SECONDS"),
    ("start_timeout", "--start-timeout SECONDS"),
    ("message", "--message TEXT | --no-message"),
)
_RESTART_TREE_FORMS = "--all-services | --only-service NAME ... | --disable-service NAME ..."


@dataclass(frozen=True)
class _RestartScope:
    only: Literal["agent-host"] | None
    all_services: bool
    only_service: Sequence[str] | None
    disable_service: Sequence[str] | None


def restart_problems(args: argparse.Namespace) -> list[str]:
    # meiva:note
    # Every missing or inapplicable `meiva restart` choice for the scope named.
    scope = options(args, _RestartScope)
    tree = bool(scope.all_services or scope.only_service or scope.disable_service)
    problems = missing_choices(args, _RESTART_COMMON_CHOICES)
    if scope.only is not None:
        if tree:
            problems.append(f"--only {scope.only} excludes {_RESTART_TREE_FORMS}")
        problems += [
            f"{form}: not part of --only {scope.only}"
            for dest, form in _RESTART_TREE_CHOICES
            if raw(args, dest) is not None
        ]
        return problems
    if not tree:
        problems.append(f"the scope: --only agent-host | {_RESTART_TREE_FORMS}")
    return problems + missing_choices(args, _RESTART_TREE_CHOICES)


@dataclass(frozen=True)
class _RestartTiming:
    # meiva:note
    # The choices every `meiva restart` scope carries once `restart_problems` is empty.
    mode: Literal["smooth", "force"]
    timeout: float
    start_timeout: float
    message: str


@dataclass(frozen=True)
class _RestartTree:
    keep_shell_sessions: bool
    keep_browser: bool
    keep_extras: bool


def h_restart(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    if problems := restart_problems(args):
        return refuse_usage("restart", problems)
    from cli.database import database_factory_of
    from cli.database.runtime import runtime_of

    scope = options(args, _RestartScope)
    timing = options(args, _RestartTiming)
    runtime = runtime_of(args)
    if scope.only is not None:
        from cli.commands.lifecycle.stop.restart import cmd_restart_agent_host

        try:
            return cmd_restart_agent_host(
                force=timing.mode == "force",
                timeout=timing.timeout,
                start_timeout=timing.start_timeout,
                message=timing.message or None,
                database_factory=database_factory_of(args),
                command=runtime,
            )
        except ValueError as error:
            return refuse_conflict("restart", error)
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    from base.paths import repo_root
    from cli.commands.lifecycle.stop.restart import cmd_restart, restart_runtime
    from cli.database import producer_of

    tree = options(args, _RestartTree)
    try:
        return cmd_restart(
            only_services=tuple(scope.only_service or ()),
            disabled_services=tuple(scope.disable_service or ()),
            all_services=scope.all_services,
            keep_shell_sessions=tree.keep_shell_sessions,
            keep_browser=tree.keep_browser,
            restart_extras=not tree.keep_extras,
            force=timing.mode == "force",
            timeout=timing.timeout,
            start_timeout=timing.start_timeout,
            message=timing.message or None,
            runtime=restart_runtime(repo_root(), home=runtime.home),
            retained_children=retained_children,
            database_factory=database_factory_of(args),
            producer=producer_of(args),
            command=runtime,
        )
    except ValueError as error:
        return refuse_conflict("restart", error)


def _h_status(args: argparse.Namespace) -> int:
    from cli.commands.lifecycle.status import cmd_status
    from cli.database import database_factory_of
    from cli.database.runtime import runtime_of

    return cmd_status(database_factory=database_factory_of(args), command=runtime_of(args))


@dataclass(frozen=True)
class _ServiceSelection:
    all_services: bool
    only_service: Sequence[str] | None
    disable_service: Sequence[str] | None


def _h_converge(args: argparse.Namespace) -> int:
    from base.paths import repo_root
    from cli.commands.lifecycle.converge_host import cmd_converge
    from cli.database import database_factory_of, producer_of
    from cli.database.runtime import runtime_of

    selection = options(args, _ServiceSelection)
    return cmd_converge(
        repo=repo_root(),
        only_services=tuple(selection.only_service or ()),
        disabled_services=tuple(selection.disable_service or ()),
        all_services=selection.all_services,
        database_factory=database_factory_of(args),
        producer=producer_of(args),
        command=runtime_of(args),
    )


def _h_firewall_status(args: argparse.Namespace) -> int:
    from cli.commands.converge.firewall_command import cmd_firewall_status
    from cli.database.runtime import runtime_of

    runtime = runtime_of(args)
    return cmd_firewall_status(
        home=runtime.home,
        user_home=runtime.session.user_home,
        machine=runtime.machine,
        host=runtime.host,
    )


def _h_firewall_sync(args: argparse.Namespace) -> int:
    from cli.commands.converge.firewall_command import cmd_firewall_sync
    from cli.database.runtime import runtime_of

    runtime = runtime_of(args)
    return cmd_firewall_sync(
        home=runtime.home,
        user_home=runtime.session.user_home,
        machine=runtime.machine,
        host=runtime.host,
    )


def add_init_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # `meiva init` — the home's identity, recorded once. NO TTY prompt — agent-first
    # design, agent has no TTY, missing values fail loud. Settings-free: it starts
    # nothing; the first `meiva start` provisions the data plane and launches.
    init_p = sub.add_parser(
        "init",
        help="[host] record this home's identity once (machine name, capabilities, "
        "credentials, ports) before its first `meiva start`; starts nothing. The home is "
        "$MEIVA_HOME, else ~/.meiva — never a flag. A home that is already initialized is "
        "refused; `meiva start` brings it up.",
    )
    init_p.add_argument(
        "--machine-name",
        default=None,
        help="stable identifier for this host (e.g. host-a / host-b); required",
    )
    init_p.add_argument(
        "--serve-gateway",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="serve the gateway capability (central pg/redis + all daemons). A single box "
        "passes both --serve-gateway --serve-agent-runner",
    )
    init_p.add_argument(
        "--serve-agent-runner",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="serve the agent-runner capability (agent-host/ops/watchdog)",
    )
    init_p.add_argument(
        "--serve-observability-station",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="serve the observability-station capability (own the native LGTM "
        "observability backends — the declarative form of the $MEIVA_HOME/lgtm-host marker)",
    )
    init_p.add_argument(
        "--machine-description",
        default=None,
        help='free-text note of what this host is for (e.g. "voice IO + browser")',
    )
    init_p.add_argument(
        "--memory-remote",
        default=None,
        help="central git remote URL for memory pool (e.g. git@github.com:you/MeivaMemory.git)",
    )
    init_p.add_argument(
        "--gateway-url",
        default=None,
        help="public URL of the gateway. On the gateway this host's own URL; on an "
        "agent-runner, the gateway it reaches (required there)",
    )
    init_p.add_argument(
        "--config-file",
        type=str,
        default=None,
        help="explicit dotenv configuration, outside the home",
    )
    init_p.add_argument(
        "--machine-host", default=None, help="this host's reachable private-network address"
    )
    init_p.add_argument("--ssl-cert-file", default=None, help="CA bundle for gateway verification")
    init_p.add_argument(
        "--db-capability",
        default=None,
        metavar="BUNDLE",
        help="agent-runner only: install the database capability bundle the gateway operator "
        "issued (`meiva cluster db-authority issue-unit`); its transport key comes from "
        "MEIVA_DB_CAPABILITY_KEY. The bundle file is deleted once installed. A later bundle "
        "goes to `meiva cluster db-authority install-unit`.",
    )
    init_p.set_defaults(func=h_init)


def add_start_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> None:
    # meiva:note
    # `meiva start` — brings up a home `meiva init` initialized; the explicit service
    # selection is its only input.
    start_p = sub.add_parser(
        "start",
        help="[host] bring up this unit's stack with the named service selection (idempotent). "
        "The home is $MEIVA_HOME, else ~/.meiva — never a flag. It must be initialized first: "
        "`meiva init` takes the machine identity (name, capabilities, gateway).",
    )
    add_service_selection(start_p)
    start_p.add_argument(
        # Internal: the OS boot job (launchd plist / systemd unit) passes it. A boot
        # start is retried by its scheduler, so it counts its attempts and stops
        # asking for a retry once the boot budget is spent
        # (`base.host.system.boot_policy`); it also never re-ups a unit whose meiva-root
        # restart budget is spent. Operators never pass it.
        "--boot",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    start_p.set_defaults(
        func=_h_start
        if retained_children is None
        else partial(_h_start, retained_children=retained_children)
    )


def add_service_selection(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    # meiva:note
    # The start selection: exactly one of the three forms; nothing is persisted.
    selection = parser.add_mutually_exclusive_group(required=required)
    selection.add_argument(
        "--all-services", action="store_true", help="select this unit's entire roster"
    )
    selection.add_argument(
        "--only-service",
        action="append",
        metavar="SERVICE",
        help="run only these services (repeatable; bare service names)",
    )
    selection.add_argument(
        "--disable-service",
        action="append",
        metavar="SERVICE",
        help="run every service but these (repeatable; e.g. --disable-service labeler "
        "--disable-service frontend)",
    )


def _add_mode_and_timeout(parser: argparse.ArgumentParser, *, timeout_help: str) -> None:
    parser.add_argument(
        "--mode",
        choices=("smooth", "force"),
        default=None,
        help="required: 'smooth' never force-kills; 'force' explicitly permits force-killing "
        "work that cannot exit normally",
    )
    parser.add_argument("--timeout", type=_positive_seconds, default=None, help=timeout_help)


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from exc
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds > 0")
    return value


def _nonblank_message(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise argparse.ArgumentTypeError("--message cannot be empty; pass --no-message instead")
    return stripped


def add_message_choice(
    parser: argparse.ArgumentParser | argparse._ArgumentGroup,
    *,
    help_text: str,
    no_message_help: str,
) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--message",
        dest="message",
        type=_nonblank_message,
        default=None,
        metavar="TEXT",
        help=help_text,
    )
    group.add_argument(
        "--no-message",
        dest="message",
        action="store_const",
        const="",
        default=None,
        help=no_message_help,
    )


def add_stop_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> None:
    stop_p = sub.add_parser(
        "stop",
        help="[host] stop this unit as the operator chooses; preserve data and agent "
        "identities. Every choice below is required (none has a default).",
    )
    add_choice(
        stop_p,
        "keep_shell_sessions",
        ("--keep-shell-sessions", "--close-shell-sessions"),
        helps=(
            "keep the pty-sessions service and every shell session it holds",
            "close every shell session, then stop the pty-sessions service",
        ),
    )
    add_choice(
        stop_p,
        "keep_browser",
        ("--keep-browser", "--stop-browser"),
        helps=("keep the shared browser (its login session) running", "stop the shared browser"),
    )
    add_choice(
        stop_p,
        "keep_extras",
        ("--keep-extras", "--stop-extras"),
        helps=(
            "keep Gate, the permissions helper and native LGTM",
            "stop Gate, the permissions helper and native LGTM",
        ),
    )
    add_choice(
        stop_p,
        "keep_infra",
        ("--keep-infra", "--stop-infra"),
        helps=(
            "gateway hosts only: keep THIS cluster's own Postgres/PgBouncer/Redis running",
            "gateway hosts only: stop THIS cluster's own Postgres/PgBouncer/Redis (data kept)",
        ),
    )
    kept = stop_p.add_mutually_exclusive_group()
    kept.add_argument(
        "--keep-service",
        action="append",
        default=None,
        metavar="SERVICE",
        help="retain this other service (repeatable); DB-dependent services require "
        "--keep-infra. Shell sessions and the browser have their own choices",
    )
    kept.add_argument(
        "--keep-no-services",
        dest="keep_service",
        action="store_const",
        const=[],
        help="retain no other service",
    )
    add_choice(
        stop_p,
        "assume_yes",
        ("--yes", "--prompt"),
        helps=("proceed without the stdin y/N confirmation", "ask y/N on stdin before stopping"),
    )
    _add_mode_and_timeout(
        stop_p, timeout_help="required: total drain/stop deadline in seconds; failure never forces"
    )
    add_message_choice(
        stop_p,
        help_text="natural-language explanation delivered to affected agents on this host "
        "(who stopped it and why)",
        no_message_help="explicitly omit an agent-facing explanation for this stop",
    )
    stop_p.add_argument(
        "--maintenance-for",
        default=None,
        metavar="DURATION",
        help="declare a planned window (30s / 15m / 2h): the stopped-too-long alert reports "
        "this host as in maintenance until then instead of as a fault",
    )
    stop_p.add_argument(
        "--reason",
        default=None,
        metavar="TEXT",
        help="one line saying why this host stops; recorded with the stop and shown in the window",
    )
    stop_p.set_defaults(
        func=h_stop
        if retained_children is None
        else partial(h_stop, retained_children=retained_children)
    )


def add_restart_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> None:
    restart_p = sub.add_parser(
        "restart",
        help="[host] stop then start, without a confirmation prompt; the data plane always "
        "stays up. Name the scope: `--only agent-host` (root brings agent-host back; only "
        "--mode, --timeout, --start-timeout and --message/--no-message apply) or a service "
        "selection (--all-services, --only-service, --disable-service) that the start leg "
        "launches, with every keep-or-restart choice.",
    )
    restart_p.add_argument(
        "--only",
        choices=("agent-host",),
        default=None,
        help="restart agent-host alone; nothing else is touched",
    )
    add_service_selection(restart_p, required=False)
    add_choice(
        restart_p,
        "keep_shell_sessions",
        ("--keep-shell-sessions", "--close-shell-sessions"),
        helps=(
            "keep the pty-sessions service and its shell sessions (they keep running old code)",
            "close every shell session; the restarted pty-sessions runs the new code",
        ),
    )
    add_choice(
        restart_p,
        "keep_browser",
        ("--keep-browser", "--restart-browser"),
        helps=("keep the shared browser running", "stop the shared browser; start relaunches it"),
    )
    add_choice(
        restart_p,
        "keep_extras",
        ("--keep-extras", "--restart-extras"),
        helps=(
            "keep Gate, the permissions helper and native LGTM",
            "stop Gate, the permissions helper and native LGTM; start brings them back",
        ),
    )
    _add_mode_and_timeout(restart_p, timeout_help="required: the stop leg's deadline in seconds")
    restart_p.add_argument(
        "--start-timeout",
        type=_positive_seconds,
        default=None,
        help="required: how long the start leg waits for the restarted services' readiness",
    )
    add_message_choice(
        restart_p,
        help_text="natural-language explanation delivered to affected agents on this host "
        "(who restarted it and why)",
        no_message_help="explicitly omit an agent-facing explanation for this restart",
    )
    restart_p.set_defaults(
        func=h_restart
        if retained_children is None
        else partial(h_restart, retained_children=retained_children)
    )


def add_status_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    status_p = sub.add_parser(
        "status",
        help="[host] one-screen view of sessions / pidfile / curl / infra / cron "
        "+ the gateway's own cluster-status snapshot",
    )
    status_p.set_defaults(func=_h_status)


def add_converge_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    converge_p = sub.add_parser(
        "converge",
        help="[host] re-apply idempotent host wiring (symlink/PATH/dirs/plugin images/memory pool); "
        "normally run automatically by meiva start. The service selection names the services "
        "whose wiring (and boot job) it converges, exactly as `meiva start` takes it",
    )
    add_service_selection(converge_p)
    converge_p.set_defaults(func=_h_converge)


def add_firewall_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    firewall_p = sub.add_parser(
        "firewall",
        help="macOS Application Firewall allowlist manifest (status / sync)",
    )
    firewall_sub = firewall_p.add_subparsers(dest="firewall_cmd", required=True)
    firewall_status_p = firewall_sub.add_parser(
        "status",
        help="audit the host + diff the allowlist manifest against ALF (read-only)",
    )
    firewall_status_p.set_defaults(func=_h_firewall_status)
    firewall_sync_p = firewall_sub.add_parser(
        "sync",
        help="apply the allowlist manifest now (rootless-first repair + prune; "
        "older macOS falls back to sudo -n/manual commands)",
    )
    firewall_sync_p.set_defaults(func=_h_firewall_sync)
