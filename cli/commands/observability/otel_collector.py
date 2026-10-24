# meiva:note
# OTel Collector sidecar install: the pinned binary download and the config generation.
#
# One collector per machine: every agent on the host sends OTLP to its local receiver (127.0.0.1,
# on MEIVA_TELEMETRY_OTLP_PORT, default the standard 4318). A gateway collector fans out to its
# loopback Tempo/Loki/Prometheus backends; a pure runner collector relays each signal to the
# gateway collector's authenticated private-address receiver. Every machine mirrors its local
# traces to JSONL. This module is the converge step's shell, idempotent: an already-installed,
# version-matching binary is a no-op, and the config is regenerated on every converge so fan-out
# and retention setting changes propagate on the next `meiva start`.
#
# The CONTRIB distribution is required, not core: the file exporter (JSONL mirror) and the
# file_storage extension (filelog read offsets) live in contrib. The version is pinned to the
# collector release the LGTM stack (deploy/lgtm) was validated with, and each platform's release
# tarball is SHA256-pinned.
#
# Remote OTLP ingress is the bearer-authenticated machine-to-machine surface
# (docs/conventions/data/reachability-and-credentials.md). What the config says and whether to
# install are decided by the modules under `plans/`; this module reads the settings, the registry
# record and the disk, resolves the station endpoint and writes the file.
# meiva:link cli/commands/observability/docs/operations.md

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from base.agents.context.clients import DatabaseFactory
from base.clock import Timers
from base.cluster.machine import MachineIdentity, MachineRoles
from base.deploy.release import collector_artifact
from base.host.net.ports import SYSTEM_URL_OPENER
from base.paths import otel_collector_config, otel_collector_dir
from base.telemetry.observability import collector_allowed_for_home, endpoint_override_is_explicit
from base.telemetry.station_endpoint import resolve_station_target, validated_observability_base
from cli.commands.converge.rendered_file import write_rendered_guarded
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.observability.inputs import CollectorSettings, collector_settings
from cli.commands.observability.plans.authority import host_port
from cli.commands.observability.plans.collector.data_plane import (
    data_plane_fragments,
    plan_data_plane_receivers,
    postgres_receiver_block,
    receiver_endpoint,
    redis_receiver_block,
)
from cli.commands.observability.plans.collector.exporters import (
    ExporterShape,
    exporter_shape,
    fanout_bases,
    render_exporters,
)
from cli.commands.observability.plans.collector.install import (
    InstallCollector,
    SkipUnsupportedPlatform,
    plan_collector_install,
    plan_collector_step,
)
from cli.commands.observability.plans.collector.remote import (
    cluster_bearer,
    gateway_ingress_endpoint,
    ingress_endpoint_problem,
    remote_receiver_fragments,
)
from cli.commands.observability.plans.collector.render import (
    CollectorValues,
    render_collector_config,
)
from cli.commands.observability.plans.collector.text import ottl_quote
from cli.commands.observability.plans.observability_config import ObservabilityConfig


def _config_template(repo: Path) -> str:
    return (repo / "deploy/otel-collector/otel-collector.yaml").read_text(encoding="utf-8")


# meiva:note
# This home's own Postgres receiver block: its socket, its port and the monitoring role.
#
# Everything comes from the home's registry record and identity; nothing is read from a database
# URL but the database name, so no credential can reach the rendered file. The cluster modules
# are imported where they are used: the converge step table imports this module on every CLI
# start.
def _postgres_block(meiva_home: Path, db_url: str) -> str:
    from base.cluster import db_identity, get_record
    from base.cluster.authority import MONITOR_ROLE
    from base.cluster.dataplane.pg_admin import pg_socket_path

    record = get_record(meiva_home)
    if record is None:
        raise RuntimeError(
            "cannot build the otel-collector postgres receiver: no registry record "
            f"for home {meiva_home}"
        )
    return postgres_receiver_block(
        socket_dir=pg_socket_path(meiva_home).as_posix(),
        port=record.ports["postgres"],
        user=MONITOR_ROLE,
        database=db_identity(db_url),
    )


# meiva:note
# The receiver blocks and the pipeline-list fragment for this unit's own data plane, or two empty
# strings for a unit that scrapes none. The Redis receiver always authenticates with the admin
# password, so the URL it was handed is rebuilt with it before the password is read back out.
# The cluster modules are imported where they are used, for the same start-up reason as
# `_postgres_block`.
def _data_plane_receivers(
    roles: MachineRoles | None, meiva_home: Path, settings: CollectorSettings
) -> tuple[str, str]:
    from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL

    plane = settings.data_plane
    plan = plan_data_plane_receivers(
        roles=roles,
        placeholder_db_url=plane.db_url == PLACEHOLDER_DB_URL,
        otlp_enabled=settings.observability.telemetry_otlp_enabled,
        remote_plane=plane.is_remote,
    )
    if not (plan.postgres or plan.redis):
        return "", ""
    from base.cluster import redis_admin_url

    admin = urlsplit(redis_admin_url(plane.redis_url, plane.redis_admin_password))
    return data_plane_fragments(
        plan,
        postgres_block=_postgres_block(meiva_home, plane.db_url) if plan.postgres else "",
        redis_block=redis_receiver_block(
            endpoint=receiver_endpoint(urlsplit(plane.redis_url), "redis"),
            password=unquote(admin.password or ""),
        ),
    )


# meiva:note
# Why `endpoint` cannot serve as the published gateway OTLP ingress, or None.
#
# The hold watchdog reads this before spending its bounded completion attempt: a gateway that has
# not published the ingress yet must not burn the attempt.
def gateway_otlp_endpoint_problem(endpoint: str) -> str | None:
    return ingress_endpoint_problem(urlsplit(endpoint.strip()))


# meiva:note
# The authenticated gateway ingress published by bootstrap, independent of local ports.
def gateway_otel_ingress_endpoint(endpoint: str) -> str:
    return gateway_ingress_endpoint(endpoint, urlsplit(endpoint.strip()))


# meiva:note
# The remote station's single ingress: the port it advertises as a machine unit, or the
# configured station port when no unit advertises one. The station's own port is used, never the
# consuming unit's receiver port. Database discovery errors propagate: rendering an invented
# target during an outage would persist it beyond recovery.
def _station_ingress(
    base: str, observability: ObservabilityConfig, database_factory: DatabaseFactory
) -> str:
    if not base:
        raise RuntimeError(
            "cannot build the remote-station OTLP relay without a valid "
            "MEIVA_OBSERVABILITY_URL (scheme://host, no port, no path)"
        )
    return resolve_station_target(
        database_factory(), base, otlp_port=observability.observability_otlp_port
    ).url


# meiva:note
# The selected station's ingress, independent of this unit's listen port.
def station_otel_ingress_endpoint(
    observability: ObservabilityConfig, *, database_factory: DatabaseFactory
) -> str:
    return _station_ingress(
        validated_observability_base(observability.observability_url),
        observability,
        database_factory,
    )


# meiva:note
# The gateway collector's Loki and Prometheus fan-out base URLs.
#
# Two-state on MEIVA_OBSERVABILITY_URL: empty (default) keeps the per-service settings URLs, whose
# defaults are this host's loopback backends; a valid value points the fan-out at the remote
# observatory station when `remote` is true. The runner relay path is unaffected: it always relays
# to the gateway collector.
def lgtm_fanout_bases(
    observability: ObservabilityConfig, *, remote: bool, database_factory: DatabaseFactory
) -> tuple[str, str]:
    base = validated_observability_base(observability.observability_url)
    station = _station_ingress(base, observability, database_factory) if base and remote else None
    return fanout_bases(
        station_endpoint=station,
        loki_url=observability.telemetry_loki_url,
        prometheus_url=observability.telemetry_prometheus_url,
    )


def _exporters(
    shape: ExporterShape,
    settings: CollectorSettings,
    *,
    bearer: str | None,
    bases: tuple[str, str],
    database_factory: DatabaseFactory,
) -> str:
    obs = settings.observability
    relay_endpoint = ""
    authorization = ""
    if shape is ExporterShape.RUNNER_RELAY:
        relay_endpoint = gateway_otel_ingress_endpoint(obs.gateway_otlp_endpoint)
        authorization = cluster_bearer(bearer)
    elif shape is ExporterShape.STATION_RELAY:
        relay_endpoint = station_otel_ingress_endpoint(obs, database_factory=database_factory)
        authorization = cluster_bearer(bearer)
    return render_exporters(
        shape,
        relay_endpoint=relay_endpoint,
        authorization=authorization,
        tempo_endpoint=obs.telemetry_tempo_endpoint,
        bases=bases,
    )


# meiva:note
# Render the sidecar config from the repo template and this unit's settings.
#
# `meiva_home` names the files the config points at (mirror, log offsets, registry record) and the
# cluster label; the telemetry bearer is derived for that home. A remote observatory and a pure
# runner relay authenticate with it, so a unit that holds no bearer fails closed rather than
# render an unauthenticated fan-out. The cluster modules are imported where they are used: the
# converge step table imports this module on every CLI start.
def generate_config(
    repo: Path,
    meiva_home: Path,
    roles: MachineRoles | None,
    *,
    settings: CollectorSettings,
    database_factory: DatabaseFactory,
    machine: MachineIdentity,
) -> str:
    from base.cluster import home_label
    from base.host.db_authority.unit import telemetry_bearer

    obs = settings.observability
    bearer = telemetry_bearer(meiva_home.resolve(), settings.data_plane.cluster_secret)
    bases = lgtm_fanout_bases(
        obs, remote=roles != frozenset({"agent-runner"}), database_factory=database_factory
    )
    receivers, pipeline = _data_plane_receivers(roles, meiva_home, settings)
    shape = exporter_shape(roles, observability_url_set=bool(obs.observability_url))
    values = CollectorValues(
        meiva_home=str(meiva_home),
        cluster_label=home_label(meiva_home),
        machine_name=ottl_quote(machine.name()),
        tempo_endpoint=obs.telemetry_tempo_endpoint.rstrip("/"),
        loki_base=bases[0],
        prom_base=bases[1],
        retention_days=obs.trace_retention_days,
        self_metrics_port=obs.otel_collector_metrics_port,
        local_ingress_endpoint=host_port("127.0.0.1", obs.telemetry_otlp_port),
        data_plane_receivers=receivers,
        data_plane_pipeline=pipeline,
        exporters=_exporters(
            shape, settings, bearer=bearer, bases=bases, database_factory=database_factory
        ),
        remote=remote_receiver_fragments(
            roles, host=machine.host(), token=bearer, ingress_port=obs.telemetry_otlp_port
        ),
    )
    return render_collector_config(_config_template(repo), values)


# meiva:note
# Publish the rendered collector config with user-edit protection.
#
# Content-hash guarded: a config.yaml the user hand-edited since the last converge write is warned
# about and preserved, never overwritten. The write itself is atomic and owner-only from first
# creation: split runners carry the cluster bearer and gateways may additionally carry data-plane
# credentials, so the file must never be exposed through the process umask (the temporary file is
# created 0600 and replacement publishes only the fully-written private file).
def write_config(path: Path, rendered: str) -> None:
    warning = write_rendered_guarded(
        path,
        rendered,
        path.parent / "rendered-hashes.json",
        path.name,
        surface="otel-collector",
    )
    if warning is not None:
        print(f"  ! otel-collector: {warning}", file=sys.stderr)


# meiva:note
# Idempotent install: the pinned binary present and the config regenerated.
#
# The binary download is skipped when the version marker matches. The config is re-rendered on
# every converge (template changes and setting changes propagate on the next converge) except a
# config.yaml the user hand-edited since the last converge write, which is warned about and
# preserved. `tag` is the release asset tag for this platform, None when there is no pinned
# collector: the sidecar is then skipped with a warning and the agent preflight disables OTLP
# export with a reported warning.
def ensure_otel_collector(
    repo: Path,
    meiva_home: Path,
    roles: MachineRoles | None,
    *,
    settings: CollectorSettings,
    database_factory: DatabaseFactory,
    machine: MachineIdentity,
    timers: Timers,
    tag: str | None,
    platform_label: str,
) -> None:
    dest_dir = otel_collector_dir(meiva_home)
    marker = dest_dir / collector_artifact.VERSION_MARKER
    plan = plan_collector_install(
        tag=tag,
        platform_label=platform_label,
        binary_present=(dest_dir / collector_artifact.binary_name()).exists(),
        marker=marker.read_text(encoding="utf-8") if marker.exists() else None,
        pinned_version=collector_artifact.OTELCOL_CONTRIB_VERSION,
    )
    if isinstance(plan, SkipUnsupportedPlatform):
        print(plan.warning(), file=sys.stderr)
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    version = collector_artifact.OTELCOL_CONTRIB_VERSION
    if isinstance(plan, InstallCollector):
        collector_artifact.download_and_verify(
            plan.tag,
            dest_dir,
            open_url=SYSTEM_URL_OPENER,
            timers=timers,
            limits=settings.download,
        )
        print(f"  · otel-collector: installed otelcol-contrib {version} ({plan.tag})")
    else:
        print(f"  · otel-collector: otelcol-contrib {version} present")
    write_config(
        otel_collector_config(meiva_home),
        generate_config(
            repo,
            meiva_home,
            roles,
            settings=settings,
            database_factory=database_factory,
            machine=machine,
        ),
    )


# meiva:note
# Install a pure-runner relay or the designated LGTM host's collector.
#
# A gateway home is skipped only when it is neither the LGTM host nor opted into an explicit
# `MEIVA_TELEMETRY_OTLP_ENDPOINT` override: the same rule as the roster gate
# (`services.supervision.roster.spec`) and the sidecar healthcheck.
def ensure_otel_collector_step(ctx: ConvergeCtx) -> None:
    gateway_role = ctx.roles is not None and "gateway" in ctx.roles
    skip = plan_collector_step(
        gateway_role=gateway_role,
        collector_allowed=not gateway_role
        or collector_allowed_for_home(
            ctx.meiva_home,
            machine=ctx.machine,
            unit_home=ctx.meiva_home,
            endpoint_explicit=endpoint_override_is_explicit(
                "MEIVA_TELEMETRY_OTLP_ENDPOINT", ctx.read_config().process_environment()
            ),
        ),
        station_marker=str(ctx.meiva_home / "lgtm-host"),
        residual_config=_existing(otel_collector_config(ctx.meiva_home)),
    )
    if skip is not None:
        for warning in skip:
            print(warning, file=sys.stderr)
        return
    view = ctx.read_config().view
    platform = ctx.host.platform
    ensure_otel_collector(
        ctx.repo,
        ctx.meiva_home,
        ctx.roles,
        settings=collector_settings(observability=view.observability, data_plane=view.data_plane),
        database_factory=ctx.database_factory,
        machine=ctx.machine,
        timers=ctx.timers,
        tag=collector_artifact.platform_tag(),
        platform_label=f"{platform.system} {platform.machine}",
    )


def _existing(path: Path) -> str | None:
    return str(path) if path.exists() else None
