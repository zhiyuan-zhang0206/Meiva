"""`meiva cluster` — whole-cluster verbs: argparse builder + its `_h_*` handlers.

Every verb here operates on the cluster as a whole (roster, recovery, health
probes, home lifecycle) rather than a single host — the
host-level set lives in ``cli.parsers.host``. Handlers lazy-import their
`cmd_*` implementation from ``cli.commands`` so parser building never loads
Settings (see ``cli.main`` module docstring)."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Literal

from base.deploy.progress_timeout import UNIT_BUNDLE_TTL_S
from cli.options import options


@dataclass(frozen=True)
class _MachineName:
    name: str


@dataclass(frozen=True)
class _MarkStagingOptions:
    name: str
    is_staging: bool


@dataclass(frozen=True)
class _PauseOptions:
    name: str
    reason: str | None


@dataclass(frozen=True)
class _DestroyOptions:
    drop_db: bool
    stop_mode: Literal["smooth", "force"]
    stop_timeout: float


@dataclass(frozen=True)
class _OpsOptions:
    hours: int
    limit: int
    operation: str | None


@dataclass(frozen=True)
class _HealthProbeOptions:
    schema_check: bool


@dataclass(frozen=True)
class _IssueUnitOptions:
    machine: str
    home: str
    out: str
    ttl_hours: float


@dataclass(frozen=True)
class _InstallUnitOptions:
    bundle: str
    transport_key: str


def _h_cluster_status(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.cluster.control import cmd_cluster_status
    from cli.database.runtime import runtime_of

    config = runtime_of(args).config
    return cmd_cluster_status(
        access=gateway_access_from_boot(config),
        status_probe_timeout_s=config.view.gateway.status_probe_timeout_seconds,
    )


def _h_cluster_mark_staging(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.cluster.control import cmd_cluster_mark_staging
    from cli.database.runtime import runtime_of

    opts = options(args, _MarkStagingOptions)
    return cmd_cluster_mark_staging(
        name=opts.name,
        is_staging=opts.is_staging,
        access=gateway_access_from_boot(runtime_of(args).config),
    )


def _h_cluster_pause(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.cluster.control import cmd_cluster_pause
    from cli.database.runtime import runtime_of

    opts = options(args, _PauseOptions)
    return cmd_cluster_pause(
        name=opts.name,
        reason=opts.reason,
        access=gateway_access_from_boot(runtime_of(args).config),
    )


def _h_cluster_resume(args: argparse.Namespace) -> int:
    from base.cluster.machine.boot import gateway_access_from_boot
    from cli.commands.cluster.control import cmd_cluster_resume
    from cli.database.runtime import runtime_of

    return cmd_cluster_resume(
        name=options(args, _MachineName).name,
        access=gateway_access_from_boot(runtime_of(args).config),
    )


def _h_cluster_destroy(args: argparse.Namespace) -> int:
    from cli.commands.cluster.home import DestroyRequest, cmd_cluster_destroy
    from cli.commands.cluster.plans.destroy_plan import StopMode
    from cli.database.runtime import runtime_of

    opts = options(args, _DestroyOptions)
    runtime = runtime_of(args)
    request = DestroyRequest(
        home=runtime.home,
        drop_db=opts.drop_db,
        stop_mode=StopMode(opts.stop_mode),
        stop_timeout_s=opts.stop_timeout,
        checkout=runtime.checkout,
        user_home=runtime.session.user_home,
        helper_port=runtime.config.view.services.permissions_helper_port,
        pid=runtime.pid,
    )
    return cmd_cluster_destroy(
        request, host=runtime.host, terminal=runtime.terminal, timers=runtime.timers
    )


# meiva:note
# `meiva cluster ops` — read the cluster's lifecycle operations from the audit record: which
# window ran (`operation_id`), who started it (`initiator`), what the release gate said
# (`gate`), and how each host ended. The rows were always written; this is the surface that
# makes them readable without hand-written SQL (task #6024).
def _h_cluster_ops(args: argparse.Namespace) -> int:
    from cli.commands.cluster.ops import cmd_cluster_ops
    from cli.database import database_factory_of

    opts = options(args, _OpsOptions)
    return cmd_cluster_ops(
        database_factory=database_factory_of(args),
        hours=opts.hours,
        limit=opts.limit,
        operation=opts.operation,
    )


def _h_cluster_health_probe(args: argparse.Namespace) -> int:
    from cli.commands.cluster.health import cmd_health_probe
    from cli.database import database_factory_of, producer_of
    from cli.database.runtime import runtime_of

    return cmd_health_probe(
        check_schema=options(args, _HealthProbeOptions).schema_check,
        database_factory=database_factory_of(args),
        producer=producer_of(args),
        command=runtime_of(args),
    )


def _h_cluster_health_probe_register(args: argparse.Namespace) -> int:
    from base.host.system.job_values import JobRoots
    from cli.commands.cluster.cron import cmd_cron_register
    from cli.database.runtime import runtime_of

    runtime = runtime_of(args)
    return cmd_cron_register(
        os_jobs_enabled=runtime.config.view.general.os_jobs_enabled,
        host=runtime.host,
        roots=JobRoots(
            home=runtime.home, repo=runtime.checkout, user_home=runtime.session.user_home
        ),
    )


def _h_cluster_health_probe_unregister(args: argparse.Namespace) -> int:
    from base.host.system.job_values import JobRoots
    from cli.commands.cluster.cron import cmd_cron_unregister
    from cli.database.runtime import runtime_of

    runtime = runtime_of(args)
    return cmd_cron_unregister(
        host=runtime.host,
        roots=JobRoots(
            home=runtime.home, repo=runtime.checkout, user_home=runtime.session.user_home
        ),
    )


def _h_cluster_db_authority_issue_unit(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_db_authority_issue_unit
    from cli.database.runtime import runtime_of

    opts = options(args, _IssueUnitOptions)
    return cmd_db_authority_issue_unit(
        machine=opts.machine,
        home=opts.home,
        out=opts.out,
        ttl_hours=opts.ttl_hours,
        command=runtime_of(args),
    )


def _h_cluster_db_authority_install_unit(args: argparse.Namespace) -> int:
    from cli.commands.cluster.control import cmd_db_authority_install_unit
    from cli.database.runtime import runtime_of

    opts = options(args, _InstallUnitOptions)
    runtime = runtime_of(args)
    return cmd_db_authority_install_unit(
        bundle=opts.bundle,
        transport_key=opts.transport_key,
        home=runtime.home,
        now=runtime.clock.now().timestamp(),
    )


def _add_db_authority_parser(
    cluster_sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    db_authority_p = cluster_sub.add_parser(
        "db-authority",
        help="[cluster] database write-generation authority for remote agent-runner units",
    )
    db_authority_sub = db_authority_p.add_subparsers(dest="db_authority_cmd", required=True)
    issue_unit_p = db_authority_sub.add_parser(
        "issue-unit",
        help="on the gateway: seal the active generation's runner login into a 0600 "
        "bundle for one agent-runner unit (join, emergencies); prints its transport key once",
    )
    issue_unit_p.add_argument("--machine", required=True, help="the unit's machine name")
    issue_unit_p.add_argument(
        "--home", required=True, help="the unit's absolute $MEIVA_HOME path on its machine"
    )
    issue_unit_p.add_argument(
        "--out", required=True, help="bundle path to create (refused when it exists)"
    )
    issue_unit_p.add_argument(
        "--ttl-hours",
        type=float,
        default=UNIT_BUNDLE_TTL_S / 3600,
        help=f"bundle lifetime in hours (default: {UNIT_BUNDLE_TTL_S / 3600:g}; "
        "the authority refuses a lifetime past its cap)",
    )
    issue_unit_p.set_defaults(func=_h_cluster_db_authority_issue_unit)
    install_unit_p = db_authority_sub.add_parser(
        "install-unit",
        help="on an initialized agent-runner unit: install a capability bundle `issue-unit` "
        "sealed for it (a fresh expiry, a rotated telemetry token); the transport key comes "
        "from MEIVA_DB_CAPABILITY_KEY and the bundle file is deleted once installed. Stop the "
        "unit first and `meiva start` it after",
    )
    install_unit_p.add_argument("bundle", metavar="BUNDLE", help="path of the sealed bundle file")
    install_unit_p.set_defaults(func=_h_cluster_db_authority_install_unit)


def add_cluster_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    # `meiva cluster status` — list machines table + per-agent-runner status_probe op
    cluster_p = sub.add_parser(
        "cluster",
        help="[cluster] cluster subcommands — every verb here operates on the whole cluster",
    )
    cluster_sub = cluster_p.add_subparsers(dest="cluster_cmd", required=True)
    cluster_status_p = cluster_sub.add_parser(
        "status",
        help="[cluster] full multi-machine roster (thin client: GET /api/cluster/roster; "
        "gateway assembles it + probes each agent-runner server-side)",
    )
    cluster_status_p.set_defaults(func=_h_cluster_status)
    for flag, help_text in (
        (
            "mark-staging",
            "[cluster] mark a machine as staging — registered + roster-visible, "
            "excluded from agent-runner fan-outs and probes. Operator-set; never touched by meiva start",
        ),
        (
            "unmark-staging",
            "[cluster] clear the staging flag — the machine becomes a normal fan-out target again",
        ),
    ):
        p_ = cluster_sub.add_parser(
            flag,
            help=help_text,
        )
        p_.add_argument("name", help="machine name (the machines-table row, e.g. the hostname)")
        p_.set_defaults(func=_h_cluster_mark_staging, is_staging=(flag == "mark-staging"))
    for verb, help_text, with_reason in (
        (
            "pause",
            "[cluster] temporarily pull a machine out of the cluster: drain its tasks "
            "(reassign in_progress to #405 with a note), terminate its agents, then "
            "hide it from roster/probe/fan-out/spawn (no offline signal). Registration "
            "kept for `meiva cluster resume`",
            True,
        ),
        (
            "resume",
            "[cluster] restore a paused machine as a normal cluster member (clears the "
            "pause latch; probing/roster/fan-out/spawn resume immediately). Prints the "
            "machine-side checklist (re-`meiva start`, pg_hba if the reachable address changed)",
            False,
        ),
    ):
        p_ = cluster_sub.add_parser(verb, help=help_text)
        p_.add_argument("name", help="machine name (the machines-table row, e.g. the hostname)")
        if with_reason:
            p_.add_argument(
                "--reason",
                default=None,
                help="free-text why the machine is being pulled out (recorded on the "
                "machines row as pause_reason for the resume checklist)",
            )
        p_.set_defaults(func=_h_cluster_pause if verb == "pause" else _h_cluster_resume)

    _add_db_authority_parser(cluster_sub)

    cluster_destroy_p = cluster_sub.add_parser(
        "destroy",
        help="[cluster] decommission this host's cluster: stop it, retire its OS jobs and mark "
        "the home detached. Asks you to type the home path, and needs an interactive terminal",
    )
    cluster_destroy_p.add_argument(
        "--drop-db",
        action="store_true",
        default=False,
        help="also remove the cluster's own pg/redis data dirs (default: keep data)",
    )
    cluster_destroy_p.add_argument(
        "--stop-mode",
        choices=("smooth", "force"),
        required=True,
        help="the stop's interruption mode, passed to `meiva stop --mode`",
    )
    cluster_destroy_p.add_argument(
        "--stop-timeout",
        type=float,
        required=True,
        metavar="SECONDS",
        help="the stop's deadline, passed to `meiva stop --timeout`",
    )
    cluster_destroy_p.set_defaults(func=_h_cluster_destroy)

    # --- `meiva cluster health-probe` ---
    cluster_health_probe_p = cluster_sub.add_parser(
        "health-probe",
        help="[cluster] assess cluster health (exit 0=healthy, 1=unhealthy); designed as a cron job payload",
    )
    # task #4092 cli-default inventory: monitoring contract — this verb is a
    # cron payload invoked bare; its defaults (the schema check on; the agent floor
    # from MEIVA_HEALTH_PROBE_AGENT_MIN) are what "healthy" means out of the box.
    cluster_health_probe_p.add_argument(
        "--no-schema-check",
        action="store_false",
        dest="schema_check",
        default=True,
        help="disable the schema health check (default: enabled)",
    )
    cluster_health_probe_p.set_defaults(func=_h_cluster_health_probe)

    # --- `meiva cluster health-probe-register` ---
    cluster_health_probe_register_p = cluster_sub.add_parser(
        "health-probe-register",
        help="register the OS-scheduled health probe (launchd on macOS, crontab on Linux)",
    )
    cluster_health_probe_register_p.set_defaults(func=_h_cluster_health_probe_register)

    # --- `meiva cluster health-probe-unregister` ---
    cluster_health_probe_unregister_p = cluster_sub.add_parser(
        "health-probe-unregister",
        help="remove the OS-scheduled health probe",
    )
    cluster_health_probe_unregister_p.set_defaults(func=_h_cluster_health_probe_unregister)

    # meiva:note
    # `meiva cluster ops` reads the lifecycle audit rows; see cli/commands/cluster/ops.py.
    cluster_ops_p = cluster_sub.add_parser(
        "ops",
        help="[cluster] read lifecycle operations: who started each window, which commit, and "
        "what the release gate said",
    )
    cluster_ops_p.add_argument(
        "--hours",
        type=int,
        default=24,
        help="how far back to read (default: 24)",
    )
    cluster_ops_p.add_argument(
        "--limit",
        type=int,
        default=200,
        help="maximum audit rows to read before grouping (default: 200)",
    )
    cluster_ops_p.add_argument(
        "--operation",
        default=None,
        metavar="ID",
        help="expand one operation id to its per-host rows",
    )
    cluster_ops_p.set_defaults(func=_h_cluster_ops)
