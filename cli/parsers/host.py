"""`meiva` host-level lifecycle verbs — argparse builders + their `_h_*` handlers.

`init` / `start` / `stop` / `restart` / `status` / `converge` / `firewall` / `trace` /
`lgtm` act
on THIS host (or the unit this checkout owns), as opposed to the cluster-wide
verbs in ``cli.parsers.cluster``. Handlers stay thin: each lazy-imports its
`cmd_*` implementation from ``cli.commands`` so building the parser never loads
Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Sequence
from functools import partial

# meiva:note
# A lifecycle verb defaults and infers nothing: every choice is the operator's
# explicit answer, and a missing answer is a usage error naming every missing
# choice at once (docs/conventions/engineering/cli-argument-discipline.md).
USAGE_EXIT = 2


def h_init(args: argparse.Namespace) -> int:
    from cli.init_intent import run_init

    return run_init(args)


def _h_start(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    from cli.start_intent import run_start

    return run_start(
        args,
        retained_children=retained_children,
        database_factory=args.database_factory,
        producer=args.producer,
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
    return [form for dest, form in choices if getattr(args, dest) is None]


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
    elif "gateway" in roles and args.keep_infra is None:
        problems.append("--keep-infra | --stop-infra (this host serves the gateway)")
    elif "gateway" not in roles and args.keep_infra is not None:
        problems.append("--keep-infra/--stop-infra: this host has no local data plane")
    return problems


def _h_stop(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    from base.cluster.machine import MachineRoleInvalid, MachineRoleMissing

    try:
        roles: frozenset[str] | None = args.runtime.machine.role()
    except (MachineRoleMissing, MachineRoleInvalid):
        roles = None
    if problems := stop_problems(args, roles):
        return refuse_usage("stop", problems)
    from cli.commands.lifecycle.stop import cmd_stop

    return cmd_stop(
        keep_infra=args.keep_infra,
        require_confirmation=not args.assume_yes,
        keep_browser=args.keep_browser,
        keep_shell_sessions=args.keep_shell_sessions,
        teardown_extras=not args.keep_extras,
        preserve_sessions=frozenset(args.keep_service),
        force=args.mode == "force",
        timeout=args.timeout,
        retained_children=retained_children,
        database_factory=args.database_factory,
        producer=args.producer,
        machine=args.runtime.machine,
        helper=args.runtime.helper,
    )


_RESTART_TREE_CHOICES = (
    ("keep_shell_sessions", "--keep-shell-sessions | --close-shell-sessions"),
    ("keep_browser", "--keep-browser | --restart-browser"),
    ("keep_extras", "--keep-extras | --restart-extras"),
)
_RESTART_COMMON_CHOICES = (
    ("mode", "--mode smooth|force"),
    ("timeout", "--timeout SECONDS"),
)
_RESTART_TREE_FORMS = "--all-services | --only-service NAME ... | --disable-service NAME ..."


def restart_problems(args: argparse.Namespace) -> list[str]:
    # meiva:note
    # Every missing or inapplicable `meiva restart` choice for the scope named.
    tree = bool(args.all_services or args.only_service or args.disable_service)
    problems = missing_choices(args, _RESTART_COMMON_CHOICES)
    if args.only is not None:
        if tree:
            problems.append(f"--only {args.only} excludes {_RESTART_TREE_FORMS}")
        problems += [
            f"{form}: not part of --only {args.only}"
            for dest, form in _RESTART_TREE_CHOICES
            if getattr(args, dest) is not None
        ]
        return problems
    if not tree:
        problems.append(f"the scope: --only agent-host | {_RESTART_TREE_FORMS}")
    return problems + missing_choices(args, _RESTART_TREE_CHOICES)


def _h_restart(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    if problems := restart_problems(args):
        return refuse_usage("restart", problems)
    if args.only is not None:
        from cli.commands.lifecycle.stop import cmd_restart_agent_host

        return cmd_restart_agent_host(
            force=args.mode == "force",
            timeout=args.timeout,
            database_factory=args.database_factory,
            command=args.runtime,
        )
    if retained_children is None:
        raise ValueError("PostgreSQL launch requires its caller-owned child retention")
    from cli.commands.lifecycle.stop import cmd_restart

    return cmd_restart(
        only_services=tuple(args.only_service or ()),
        disabled_services=tuple(args.disable_service or ()),
        all_services=args.all_services,
        keep_shell_sessions=args.keep_shell_sessions,
        keep_browser=args.keep_browser,
        restart_extras=not args.keep_extras,
        force=args.mode == "force",
        timeout=args.timeout,
        retained_children=retained_children,
        database_factory=args.database_factory,
        producer=args.producer,
        command=args.runtime,
    )


def _h_status(args: argparse.Namespace) -> int:
    if args.json:
        from cli.commands.lifecycle.hold_report import cmd_status_json

        return cmd_status_json()
    from cli.commands.lifecycle.status import cmd_status

    return cmd_status(
        database_factory=args.database_factory,
        machine=args.runtime.machine,
        config=args.runtime.config,
        host=args.runtime.host,
    )


def _h_converge(args: argparse.Namespace) -> int:
    from cli.commands.lifecycle.converge_host import cmd_converge

    return cmd_converge(
        database_factory=args.database_factory, producer=args.producer, command=args.runtime
    )


def _h_firewall_status(args: argparse.Namespace) -> int:
    from cli.commands.converge.firewall_command import cmd_firewall_status

    return cmd_firewall_status(machine=args.runtime.machine, host=args.runtime.host)


def _h_firewall_sync(args: argparse.Namespace) -> int:
    from cli.commands.converge.firewall_command import cmd_firewall_sync

    return cmd_firewall_sync(machine=args.runtime.machine, host=args.runtime.host)


def _h_trace_ship(args: argparse.Namespace) -> int:
    from base.paths import meiva_home, traces_dir
    from cli.commands.observability.trace import cmd_trace_ship

    return cmd_trace_ship(
        since=args.since,
        until=args.until,
        dry_run=args.dry_run,
        machine=args.runtime.machine,
        traces_dir=traces_dir(meiva_home()),
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
    stop_p.set_defaults(
        func=_h_stop
        if retained_children is None
        else partial(_h_stop, retained_children=retained_children)
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
        "--mode and --timeout apply) or a service selection (--all-services, --only-service, "
        "--disable-service) that the start leg launches, with every keep-or-restart choice.",
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
    restart_p.set_defaults(
        func=_h_restart
        if retained_children is None
        else partial(_h_restart, retained_children=retained_children)
    )


def add_status_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    status_p = sub.add_parser(
        "status",
        help="[host] one-screen view of sessions / pidfile / curl / infra / cron "
        "+ the gateway's own cluster-status snapshot",
    )
    status_p.add_argument(
        "--json",
        action="store_true",
        help="print only this unit's maintenance hold as one JSON object "
        "(reads the local journal; probes nothing)",
    )
    status_p.set_defaults(func=_h_status)


def add_converge_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    converge_p = sub.add_parser(
        "converge",
        help="[host] re-apply idempotent host wiring (symlink/PATH/dirs/plugin images/memory pool); "
        "normally run automatically by meiva start",
    )
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


def _h_lgtm(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    from cli.commands.lifecycle.lgtm_toggle import cmd_lgtm_off, cmd_lgtm_on
    from cli.commands.observability.lgtm import cmd_lgtm_status

    if args.lgtm_cmd == "on":
        return cmd_lgtm_on(
            retained_children=retained_children,
            database_factory=args.database_factory,
            producer=args.producer,
            command=args.runtime,
        )
    if args.lgtm_cmd == "off":
        return cmd_lgtm_off(
            retained_children=retained_children,
            database_factory=args.database_factory,
            producer=args.producer,
            command=args.runtime,
        )
    if args.lgtm_cmd == "render":
        from cli.commands.observability.grafana_render import cmd_grafana_render

        return cmd_grafana_render(
            force=args.force, repo_only=args.repo_only, database_factory=args.database_factory
        )
    return cmd_lgtm_status(machine=args.runtime.machine)


def add_lgtm_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> None:
    # `meiva lgtm on|off|status` — the observability-stack toggle on THIS host.
    # One command each way so observability's own overhead is measurable:
    # `off` removes the $MEIVA_HOME/lgtm-host marker (converge + watchdog stop
    # touching the stack) and compose-downs the containers (volumes persist);
    # `on` restores marker + stack with history intact.
    lgtm_p = sub.add_parser(
        "lgtm",
        help="observability stack (Loki/Grafana/Tempo/Prometheus) on/off/status on this host",
    )
    lgtm_sub = lgtm_p.add_subparsers(dest="lgtm_cmd", required=True)
    for name, help_text in (
        ("on", "designate this host as the LGTM host + bring the stack up (idempotent)"),
        ("off", "take the stack down + stop being the LGTM host (volumes persist)"),
        ("status", "marker + containers + readiness probes"),
    ):
        p = lgtm_sub.add_parser(name, help=help_text)
        p.set_defaults(
            func=_h_lgtm
            if retained_children is None or name == "status"
            else partial(_h_lgtm, retained_children=retained_children)
        )
    render_p = lgtm_sub.add_parser(
        "render",
        help="render the meiva-ops dashboard from the metric registries and diff it "
        "against this host's provisioning copy (--force writes the render)",
    )
    render_p.add_argument(
        "--force",
        action="store_true",
        help="write the render into this host's provisioning tree (atomic; the "
        "diff preview is the default)",
    )
    render_p.add_argument(
        "--repo-only",
        action="store_true",
        help="skip the installed-plugin registry read — render the checkout's "
        "plugins only (for a preview on a host without a reachable database)",
    )
    render_p.set_defaults(func=_h_lgtm)


def add_trace_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # `meiva trace ship` — replay the local OTel trace mirror to Tempo over OTLP
    trace_p = sub.add_parser("trace", help="trace mirror subcommands")
    trace_sub = trace_p.add_subparsers(dest="trace_cmd", required=True)
    trace_ship_p = trace_sub.add_parser(
        "ship",
        help="replay the local $MEIVA_HOME/traces mirror to Tempo over OTLP "
        "(incremental from a per-file watermark; --since/--until re-ships a window)",
    )
    trace_ship_p.add_argument(
        "--since",
        default=None,
        help="ship files dated on/after this day (YYYY-MM-DD); ignores watermark",
    )
    trace_ship_p.add_argument(
        "--until",
        default=None,
        help="ship files dated on/before this day (YYYY-MM-DD); ignores watermark",
    )
    trace_ship_p.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="count what would ship without POSTing",
    )
    trace_ship_p.set_defaults(func=_h_trace_ship)
