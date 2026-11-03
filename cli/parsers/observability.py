# meiva:note
# `meiva` host-level observability verbs — `lgtm` and `trace`.
#
# Handlers stay thin and lazy-import their `cmd_*` implementations so building
# the parser never loads Settings.

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from functools import partial
from typing import Literal

from cli.boundary.options import options
from cli.parsers.host import ServiceSelection, add_service_selection, refuse_usage


@dataclass(frozen=True)
class _LgtmVerb:
    lgtm_cmd: Literal["on", "off", "status", "render"]


@dataclass(frozen=True)
class _LgtmRenderOptions:
    force: bool
    repo_only: bool


@dataclass(frozen=True)
class _TraceShipOptions:
    since: str | None
    until: str | None
    dry_run: bool


def _h_lgtm(
    args: argparse.Namespace,
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> int:
    from base.paths import repo_root
    from base.telemetry.lgtm_local import LgtmSettings
    from cli.commands.lifecycle.lgtm_toggle import cmd_lgtm_off, cmd_lgtm_on, selection_problem
    from cli.commands.observability.lgtm import cmd_lgtm_status
    from cli.database import database_factory_of, producer_of
    from cli.database.runtime import delivered_config, runtime_of
    from cli.start_runtime import StartRuntime

    verb = options(args, _LgtmVerb).lgtm_cmd
    if verb in ("on", "off"):
        if retained_children is None:
            raise ValueError("PostgreSQL launch requires its caller-owned child retention")
        selection = options(args, ServiceSelection)
        only = tuple(selection.only_service or ())
        disabled = tuple(selection.disable_service or ())
        enabled = verb == "on"
        problem = selection_problem(enabled=enabled, only_services=only, disabled_services=disabled)
        if problem is not None:
            return refuse_usage(f"lgtm {verb}", [problem])
        return (cmd_lgtm_on if enabled else cmd_lgtm_off)(
            only_services=only,
            disabled_services=disabled,
            all_services=selection.all_services,
            runtime=StartRuntime.admit(repo_root()),
            retained_children=retained_children,
            database_factory=database_factory_of(args),
            producer=producer_of(args),
            command=runtime_of(args),
        )
    if verb == "render":
        from cli.commands.observability.grafana_render import cmd_grafana_render

        render = options(args, _LgtmRenderOptions)
        return cmd_grafana_render(
            home=runtime_of(args).home,
            force=render.force,
            repo_only=render.repo_only,
            database_factory=database_factory_of(args),
        )
    runtime = runtime_of(args)
    return cmd_lgtm_status(
        home=runtime.home,
        machine=runtime.machine,
        observability=LgtmSettings.from_fields(delivered_config(runtime.config).get_field),
    )


def _h_trace_ship(args: argparse.Namespace) -> int:
    from base.paths import traces_dir
    from cli.commands.observability.trace import cmd_trace_ship
    from cli.database.runtime import runtime_of

    opts = options(args, _TraceShipOptions)
    runtime = runtime_of(args)
    return cmd_trace_ship(
        since=opts.since,
        until=opts.until,
        dry_run=opts.dry_run,
        machine=runtime.machine,
        traces_dir=traces_dir(runtime.home),
        config=runtime.config,
        home=runtime.home,
    )


def add_lgtm_parser(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
    *,
    retained_children: list[subprocess.Popen[bytes]] | None = None,
) -> None:
    # meiva:note
    # `meiva lgtm on|off|status` toggles the observability stack on THIS host:
    # `off` removes the $MEIVA_HOME/lgtm-host marker and compose-downs the
    # containers (volumes persist); `on` restores marker + stack with history intact.
    lgtm_p = sub.add_parser(
        "lgtm",
        help="observability stack (Loki/Grafana/Tempo/Prometheus) on/off/status on this host",
    )
    lgtm_sub = lgtm_p.add_subparsers(dest="lgtm_cmd", required=True)
    for name, help_text in (
        (
            "on",
            "designate this host as the LGTM host and start the named service selection, "
            "which must include the LGTM backends (idempotent)",
        ),
        (
            "off",
            "stop being the LGTM host and start the named service selection without the "
            "backends (volumes persist)",
        ),
        ("status", "marker + containers + readiness probes"),
    ):
        p = lgtm_sub.add_parser(name, help=help_text)
        if name != "status":
            add_service_selection(p)
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
    # meiva:note
    # `meiva trace ship` replays the local OTel trace mirror to Tempo over OTLP.
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
