# meiva:note
# Prepare native LGTM assets and configuration for the root service roster.
#
# Preparation never registers OS jobs, launches services or retires another home. The pinned
# versions, the install plan, the rendered text and the warnings are decided by the modules under
# `plans/`; this module reads the settings and the disk, downloads, extracts, writes and runs the
# pinned Loki parser.

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import assert_never, cast

import yaml
from dotenv import dotenv_values

from base.agents.context.clients import DatabaseFactory
from base.cluster.machine import MachineIdentityReader
from base.host.net.ports import SYSTEM_URL_OPENER
from base.host.net.resilience import Policy, retry
from base.host.system.hostport import Host
from base.telemetry import EventPipeline
from base.telemetry.lgtm_local import BACKENDS, binary_path
from base.telemetry.lgtm_local import storage_dir as _storage_dir
from base.telemetry.loki_index_labels import validate_loki_deploy_config
from base.telemetry.station_endpoint import validated_observability_base
from cli.commands.converge.spec import ConvergeCtx
from cli.commands.observability._lgtm_assets import NATIVE_CONSTANTS, load_versions
from cli.commands.observability._lgtm_provisioning import render_dashboard, render_provisioning
from cli.commands.observability.inputs import LgtmSettings, lgtm_settings_from_boot
from cli.commands.observability.plans.lgtm.archive import (
    ExtractMember,
    ExtractTree,
    checksum_problem,
    extraction_plan,
    install_markers,
    loki_verify_argv,
    loki_verify_error,
)
from cli.commands.observability.plans.lgtm.dashboard_plan import DashboardOutcome
from cli.commands.observability.plans.lgtm.download_report import (
    attempt_failure_line,
    budget_exceeded_message,
    done_line,
    give_up_message,
    progress_line,
)
from cli.commands.observability.plans.lgtm.download_retry import (
    AttemptState,
    DownloadLimits,
    content_length,
    progress_due,
    retry_wait,
)
from cli.commands.observability.plans.lgtm.install import (
    InstallBackend,
    InstalledMarkers,
    KeepBackend,
    plan_backend_installs,
    platform_tag,
    require_backend_selection,
    station_backends,
)
from cli.commands.observability.plans.lgtm.render import (
    CONFIG_TEMPLATES,
    NativeRenderValues,
    ReadSurface,
    env_divergences,
    listen_read_warnings,
    render_config,
    render_run_script,
    tempo_topology_warning,
)
from cli.commands.observability.plans.lgtm.urls import (
    alerts_webhook,
    datasource_urls,
    normalize_gateway_base,
    pg_datasource,
)
from cli.commands.observability.url_split import split_url


# meiva:note
# The pinned versions and per-platform archives for the release tag, from the checkout's
# `versions.yml`.
def native_versions(repo: Path, *, tag: str) -> dict[str, dict[str, str]]:
    return load_versions(repo, tag)


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


# meiva:link cli/commands/observability/docs/lgtm/lgtm.meiva.okf.md
def stream_download(
    url: str, destination: Path, *, limits: DownloadLimits, deadline: float
) -> None:
    received = 0
    last_report = 0.0

    def require_budget() -> float:
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise TimeoutError(
                budget_exceeded_message(limits, received=received, remaining_s=remaining)
            )
        return remaining

    def elapsed() -> float:
        return limits.total_budget_s - _remaining(deadline)

    with (
        SYSTEM_URL_OPENER(url, min(limits.socket_timeout_s, require_budget())) as response,
        destination.open("wb") as output,
    ):
        total = content_length(response.headers.get("Content-Length"))
        while True:
            require_budget()
            chunk = response.read1(1 << 16)
            require_budget()
            if not chunk:
                break
            output.write(chunk)
            received += len(chunk)
            if progress_due(limits, elapsed_s=elapsed(), last_report_s=last_report):
                last_report = elapsed()
                print(progress_line(received=received, total=total, elapsed_s=last_report))
    print(done_line(received=received, elapsed_s=elapsed()))


def _bytes_on_disk(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


# meiva:note
# Every failure of one attempt is retriable here: network and filesystem failures alike.
def _always_retriable(_error: Exception) -> bool:
    return True


# meiva:note
# The shared retry policy of one download: the configured attempts, the linear backoff cut to the
# budget that remains at the moment it is asked for, and no jitter (one mirror, one caller).
def _download_policy(limits: DownloadLimits, *, deadline: float) -> Policy:
    def backoff(attempt: int) -> float:
        return retry_wait(limits, attempt=attempt, remaining_s=_remaining(deadline))

    return Policy(
        max_attempts=limits.attempts,
        backoff=backoff,
        jitter="none",
        jitter_span=1.0,
        classify=_always_retriable,
        idempotent=True,
        respect_retry_after=False,
        on_final_failure=None,
    )


# meiva:note
# Download `url` to `archive`, retrying until the attempts or the single deadline are spent.
#
# Each attempt starts from an empty file, so the bytes reported for a failed attempt are the ones
# that attempt received.
# meiva:link cli/commands/observability/docs/lgtm/lgtm.meiva.okf.md
def download_with_retry(url: str, archive: Path, *, limits: DownloadLimits) -> None:
    started = time.monotonic()
    deadline = started + limits.total_budget_s
    attempt = 0

    def state() -> AttemptState:
        return AttemptState(
            attempt=attempt,
            used_s=time.monotonic() - started,
            remaining_s=_remaining(deadline),
            received=_bytes_on_disk(archive),
        )

    def download_once() -> None:
        nonlocal attempt
        attempt += 1
        archive.unlink(missing_ok=True)
        try:
            stream_download(url, archive, limits=limits, deadline=deadline)
        except Exception as exc:
            print(attempt_failure_line(limits, state(), error=str(exc)), file=sys.stderr)
            raise

    try:
        retry(_download_policy(limits, deadline=deadline))(download_once)
    except Exception as exc:
        raise RuntimeError(give_up_message(limits, state(), url=url, error=str(exc))) from exc


# meiva:note
# Copy the expected release member into its final binary path.
def _copy_zip_member(archive: Path, member: str, target: Path) -> None:
    with (
        zipfile.ZipFile(archive) as bundle,
        bundle.open(member) as source,
        target.open("wb") as output,
    ):
        shutil.copyfileobj(source, output)


def _copy_tar_member(archive: Path, member: str, target: Path) -> None:
    with tarfile.open(archive) as bundle:
        source = bundle.extractfile(member)
        if source is None:
            raise KeyError(member)
        with source, target.open("wb") as output:
            shutil.copyfileobj(source, output)


def _extract_member(name: str, archive: Path, destination: Path, member: str) -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        extracted = Path(temporary_dir) / name
        copy = _copy_zip_member if archive.suffix == ".zip" else _copy_tar_member
        try:
            copy(archive, member, extracted)
        except KeyError as exc:
            raise RuntimeError(f"native LGTM {name} archive has no {member} member") from exc
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted), destination)
    destination.chmod(0o755)


# meiva:note
# Install a release archive tree after stripping its top-level directory.
def _extract_tree(name: str, archive: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        extracted = Path(temporary_dir) / "extracted"
        extracted.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(extracted, filter="data")
        roots = [path for path in extracted.iterdir() if path.is_dir()]
        if len(roots) != 1:
            raise RuntimeError(f"native LGTM {name} archive must have one top-level directory")
        if destination.exists():
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(roots[0]), destination)


# meiva:note
# Download, hash-verify and install one pinned native backend.
def download_and_verify(
    name: str,
    version: str,
    asset: dict[str, str],
    native_dir: Path,
    *,
    tag: str,
    limits: DownloadLimits,
) -> None:
    with tempfile.TemporaryDirectory() as temporary_dir:
        archive = Path(temporary_dir) / Path(asset["url"]).name
        print(f"  · lgtm native: downloading {name} {version} from {asset['url']}")
        download_with_retry(asset["url"], archive, limits=limits)
        problem = checksum_problem(
            name,
            version,
            actual=hashlib.sha256(archive.read_bytes()).hexdigest(),
            expected=asset["sha256"],
        )
        if problem is not None:
            raise RuntimeError(problem)
        service = NATIVE_CONSTANTS[name]
        extraction = extraction_plan(
            archive_member=service.archive_member,
            asset_member=asset["member"],
            binary_path=service.binary_path,
        )
        match extraction:
            case ExtractTree(destination=destination):
                _extract_tree(name, archive, native_dir / destination)
            case ExtractMember(member=member, destination=destination):
                _extract_member(name, archive, native_dir / destination, member)
            case _:
                assert_never(extraction)
    for filename, content in install_markers(name, version=version, tag=tag):
        (native_dir / filename).write_text(content, encoding="utf-8")


# meiva:note
# Publish text only when it differs, avoiding needless launchd churn. A `mode` creates the file
# owner-restricted from the start and corrects an existing file's mode even when the text is
# unchanged.
def write_if_changed(path: Path, content: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        if mode is not None:
            path.chmod(mode)
        return
    if mode is None:
        path.write_text(content, encoding="utf-8")
        return
    if path.exists():
        path.chmod(mode)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), mode)
        output.write(content)


# meiva:note
# Print the divergences between the values about to be rendered and what the unit's `.env`
# declares. A home without a `.env` has nothing to diverge from.
def warn_env_file_divergence(meiva_home: Path, values: dict[str, str]) -> None:
    env_file = meiva_home / ".env"
    if not env_file.exists():
        return
    for warning in env_divergences(dotenv_values(env_file), values, env_file=str(env_file)):
        print(warning, file=sys.stderr)


def _render_warnings(settings: LgtmSettings) -> tuple[str, ...]:
    obs = settings.observability
    query_url = obs.telemetry_tempo_query_url.rstrip("/")
    intake_endpoint = obs.telemetry_tempo_endpoint.rstrip("/")
    topology = tempo_topology_warning(
        query_url=query_url,
        query=split_url(query_url),
        intake_endpoint=intake_endpoint,
        intake=split_url(intake_endpoint),
    )
    surfaces = tuple(
        ReadSurface(backend, env_var, listen_host, read_url, split_url(read_url))
        for backend, env_var, listen_host, read_url in (
            ("loki", "MEIVA_TELEMETRY_LOKI_URL", obs.lgtm_listen_host, obs.telemetry_loki_url),
            (
                "prometheus",
                "MEIVA_TELEMETRY_PROMETHEUS_URL",
                obs.lgtm_listen_host,
                obs.telemetry_prometheus_url,
            ),
            (
                "grafana",
                "MEIVA_TELEMETRY_GRAFANA_URL",
                obs.lgtm_grafana_listen_host,
                obs.telemetry_grafana_url,
            ),
        )
    )
    return (*((topology,) if topology else ()), *listen_read_warnings(surfaces))


# meiva:note
# The datasource and webhook URLs Grafana's provisioning reads, resolved from the settings, the
# machine and the cluster's direct database URL; the stderr notices they raise are printed here.
#
# libpq's parser is imported where it is used: the converge step table imports this module on
# every CLI start, and only a station render needs it.
def _grafana_endpoints(
    settings: LgtmSettings, *, direct_db_url: str, machine: MachineIdentityReader
) -> tuple[str, str, str, str]:
    obs = settings.observability
    base = validated_observability_base(obs.observability_url)
    loki_url, prometheus_url = datasource_urls(
        observatory_base=base,
        loki_url=obs.telemetry_loki_url,
        prometheus_url=obs.telemetry_prometheus_url,
    )
    from psycopg.conninfo import conninfo_to_dict

    connection = conninfo_to_dict(direct_db_url)
    pg = pg_datasource(
        str(connection["host"]) if connection.get("host") else None,
        str(connection["port"]) if connection.get("port") else None,
        remote_observatory=bool(base),
    )
    if pg.warning is not None:
        print(pg.warning, file=sys.stderr)
    gateway_base = normalize_gateway_base(settings.webhook.gateway_url)
    webhook = alerts_webhook(
        remote_observatory=bool(obs.observability_url),
        gateway_base=gateway_base,
        gateway=split_url(gateway_base),
        gateway_port=settings.webhook.gateway_port,
        machine_host=machine.host(),
    )
    if webhook.notice is not None:
        print(webhook.notice, file=sys.stderr)
    return loki_url, prometheus_url, pg.host_port, webhook.url


# meiva:note
# Render the native templates from this checkout and the settings into `native_dir`.
#
# `direct_db_url` is the cluster's own Postgres URL (the Grafana SQL datasource derives its
# `host:port` from it, never its credentials) and `dashboard` the outcome of the `meiva-ops`
# render; both are effects the caller already performed. Grafana provisioning is converge-rendered
# (`datasources.yml` and `contact.yml` carry settings-baked URLs); Grafana reads the rendered tree,
# never the source checkout. The Loki config is checked against the deploy invariants before any
# file is written.
def render_configs(
    repo: Path,
    native_dir: Path,
    meiva_home: Path,
    *,
    settings: LgtmSettings,
    storage_dir: Path,
    direct_db_url: str,
    dashboard: DashboardOutcome,
    producer: Callable[[], EventPipeline],
    machine: MachineIdentityReader,
) -> None:
    source_dir = repo / "deploy/lgtm/native/config"
    obs = settings.observability
    tempo_query_url = obs.telemetry_tempo_query_url.rstrip("/")
    for warning in _render_warnings(settings):
        print(warning, file=sys.stderr)
    warn_env_file_divergence(
        meiva_home,
        {
            "MEIVA_TELEMETRY_TEMPO_QUERY_URL": tempo_query_url,
            "MEIVA_TELEMETRY_TEMPO_ENDPOINT": obs.telemetry_tempo_endpoint.rstrip("/"),
            "MEIVA_TELEMETRY_LOKI_URL": obs.telemetry_loki_url,
            "MEIVA_TELEMETRY_PROMETHEUS_URL": obs.telemetry_prometheus_url,
            "MEIVA_TELEMETRY_GRAFANA_URL": obs.telemetry_grafana_url,
            "MEIVA_OBSERVABILITY_URL": obs.observability_url,
            "MEIVA_LGTM_LISTEN_HOST": obs.lgtm_listen_host,
            "MEIVA_LGTM_GRAFANA_LISTEN_HOST": obs.lgtm_grafana_listen_host,
            "MEIVA_LGTM_LOKI_PORT": str(obs.lgtm_loki_port),
            "MEIVA_LGTM_GRAFANA_PORT": str(obs.lgtm_grafana_port),
        },
    )
    loki_url, prometheus_url, pg_url, webhook_url = _grafana_endpoints(
        settings, direct_db_url=direct_db_url, machine=machine
    )
    values = NativeRenderValues(
        meiva_home=str(meiva_home),
        storage_dir=str(storage_dir),
        provisioning_path=str(native_dir / "config" / "provisioning"),
        repo=str(repo),
        tempo_query_url=tempo_query_url,
        loki_url=loki_url,
        prometheus_url=prometheus_url,
        pg_url=pg_url,
        alerts_webhook_url=webhook_url,
        telemetry_loki_url=obs.telemetry_loki_url,
        telemetry_prometheus_url=obs.telemetry_prometheus_url,
        telemetry_grafana_url=obs.telemetry_grafana_url,
        listen_host=obs.lgtm_listen_host,
        grafana_listen_host=obs.lgtm_grafana_listen_host,
        loki_port=obs.lgtm_loki_port,
        loki_grpc_port=obs.lgtm_loki_grpc_port,
        grafana_port=obs.lgtm_grafana_port,
    )
    rendered = {
        name: render_config(name, (source_dir / name).read_text(encoding="utf-8"), values)
        for name in CONFIG_TEMPLATES
    }
    loki_config = yaml.safe_load(rendered["loki.yaml"])
    if not isinstance(loki_config, dict):
        raise TypeError("native Loki config must render to a mapping")
    validate_loki_deploy_config(cast(dict[str, object], loki_config))
    for name, content in rendered.items():
        write_if_changed(native_dir / "config" / name, content)
    rendered_run_script = native_dir / "grafana" / "run.sh"
    write_if_changed(
        rendered_run_script,
        render_run_script((source_dir / "run.sh").read_text(encoding="utf-8"), values),
    )
    rendered_run_script.chmod(0o755)
    render_provisioning(repo, native_dir, dashboard=dashboard, producer=producer)


# meiva:note
# Render the host-scoped Grafana credential when one is configured.
def render_grafana_admin_password(native_dir: Path, password: str | None) -> None:
    if password is None:
        return
    write_if_changed(native_dir / "grafana/admin_password", password + "\n", mode=0o600)


# meiva:note
# Render the direct Telegram contact point's bot token and chat id when this home has a bot.
#
# The gateway's own alert path (webhook, ingest, im_bridge) cannot report that the gateway is
# down; Grafana's Telegram notifier does not depend on it. Without a bot the file is removed and
# `run.sh` falls back to inert placeholders.
def render_grafana_telegram_env(native_dir: Path, *, token: str, owner: int) -> None:
    env_file = native_dir / "grafana/telegram.env"
    if not token or not owner:
        env_file.unlink(missing_ok=True)
        return
    content = (
        f"export MEIVA_ALERTS_TELEGRAM_BOT_TOKEN={shlex.quote(token)}\n"
        f"export MEIVA_ALERTS_TELEGRAM_CHAT_ID={owner}\n"
    )
    write_if_changed(env_file, content, mode=0o600)


def _installed_markers(native_dir: Path, names: tuple[str, ...]) -> dict[str, InstalledMarkers]:
    def read(path: Path) -> str | None:
        return path.read_text(encoding="utf-8").strip() if path.exists() else None

    return {
        name: InstalledMarkers(
            version=read(native_dir / f"version-{name}"),
            platform_tag=read(native_dir / f"platform-{name}"),
        )
        for name in names
    }


# meiva:note
# Prepare the selected pinned binaries and config; lifecycle belongs to meiva-root.
#
# `storage_dir` is the retained observation data volume, resolved by the composition root from
# `MEIVA_LGTM_STORAGE_DIR`. The cluster's database URL and the dashboard render are read here
# once and handed to the renderer as values.
def ensure_lgtm_native(
    repo: Path,
    meiva_home: Path,
    *,
    services: frozenset[str],
    settings: LgtmSettings,
    storage_dir: Path,
    database_factory: DatabaseFactory,
    producer: Callable[[], EventPipeline],
    machine: MachineIdentityReader,
    host: Host,
) -> None:
    require_backend_selection(services, BACKENDS)
    tag = platform_tag(host.platform.system, host.platform.machine)
    if tag is None:
        raise RuntimeError("Native LGTM has no pinned binaries for this platform")
    native_dir = meiva_home / "lgtm/native"
    for directory in ("bin", "config", "logs"):
        (native_dir / directory).mkdir(parents=True, exist_ok=True)
    for sub in ("", "loki", "prom"):
        (storage_dir / sub).mkdir(parents=True, exist_ok=True)
    assets = native_versions(repo, tag=tag)
    plan = plan_backend_installs(
        tag=tag,
        pinned_versions={name: asset["version"] for name, asset in assets.items()},
        installed=_installed_markers(native_dir, tuple(assets)),
        services=services,
    )
    for step in plan:
        asset = assets[step.name]
        match step:
            case KeepBackend():
                print(f"  · lgtm native: {step.name} {asset['version']} present")
            case InstallBackend():
                download_and_verify(
                    step.name,
                    asset["version"],
                    asset,
                    native_dir,
                    tag=tag,
                    limits=settings.download,
                )
                print(f"  · lgtm native: installed {step.name} {asset['version']} ({tag})")
            case _:
                assert_never(step)
    render_configs(
        repo,
        native_dir,
        meiva_home,
        settings=settings,
        storage_dir=storage_dir,
        direct_db_url=database_factory().direct_resolution().url,
        dashboard=render_dashboard(database_factory),
        producer=producer,
        machine=machine,
    )
    if "grafana" in services:
        render_grafana_admin_password(native_dir, settings.credentials.admin_password)
        render_grafana_telegram_env(
            native_dir,
            token=settings.credentials.telegram_bot_token,
            owner=settings.credentials.telegram_owner_id,
        )
    if "loki" in services:
        verify_loki(meiva_home, host=host)


# meiva:note
# Use the pinned binary's own parser before starting the root generation.
def verify_loki(home: Path, *, host: Host) -> None:
    argv = loki_verify_argv(str(binary_path(home, "loki")), str(home.resolve()))
    result = host.run(list(argv), capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError(loki_verify_error(result.stderr))


# meiva:note
# Converge the native backends on the observability station.
#
# Provider identity is the legacy `lgtm-host` marker or the declarative `observability-station`
# capability; both install the pinned binaries and render the configs and storage dirs of the
# selected backends. Starting them belongs to `meiva-root`.
def ensure_lgtm_native_step(ctx: ConvergeCtx) -> None:
    selected = station_backends(
        marker_present=(ctx.meiva_home / "lgtm-host").exists(),
        roles=ctx.roles,
        services=ctx.services,
        backends=BACKENDS,
    )
    if not selected:
        return
    ensure_lgtm_native(
        ctx.repo,
        ctx.meiva_home,
        services=selected,
        settings=lgtm_settings_from_boot(ctx.read_config()),
        storage_dir=_storage_dir(ctx.meiva_home),
        database_factory=ctx.database_factory,
        producer=ctx.producer,
        machine=ctx.machine,
        host=ctx.host,
    )
