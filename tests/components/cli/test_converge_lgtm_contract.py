# meiva:note
# Contract: the rendered LGTM configs equal the repository's deploy/lgtm provisioning files, and the
# native Loki template ships the noise-reducing log level.

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml

from base import config
from base.cluster.machine import MachineIdentityReader
from base.telemetry import EventPipeline
from base.telemetry.lgtm_local import LgtmSettings as LocalLgtmSettings
from base.telemetry.lgtm_local import service_argv, storage_dir
from cli.commands.observability import lgtm_native
from cli.commands.observability.inputs import LgtmSettings
from cli.commands.observability.plans.lgtm.dashboard_plan import RenderedDashboard
from cli.commands.observability.tests.observability_inputs import lgtm_settings, observability
from cli.database.tests.command_inputs import command_machine as command_machine
from cli.database.tests.command_inputs import command_runtime as command_runtime
from tests.path_scoped.cli_tests import operator_pipeline as operator_pipeline

_STUB_RENDER = '{"title": "Meiva Ops", "panels": []}\n'

# meiva:note
# The deployment these cases render for: a loopback Postgres on 5433, whose host:port the Grafana
# SQL datasource carries.
_DIRECT_DB_URL = "postgresql://reader@127.0.0.1:5433/meiva"


# meiva:note
# Render the native configs for the observability values these cases pin, over the default gateway
# (no base URL, port 8000) and the loopback database.
def _render_configs(
    repo: Path,
    native_dir: Path,
    home: Path,
    *,
    settings: LgtmSettings,
    producer: Callable[[], EventPipeline],
    machine: MachineIdentityReader,
) -> None:
    lgtm_native.render_configs(
        repo,
        native_dir,
        home,
        settings=settings,
        storage_dir=storage_dir(home, LocalLgtmSettings.from_fields(config.get_field)),
        direct_db_url=_DIRECT_DB_URL,
        dashboard=RenderedDashboard(_STUB_RENDER, ()),
        producer=producer,
        machine=machine,
    )


# meiva:note
# The native Loki and Prometheus listeners are rendered from
# `LocalLgtmSettings.from_fields(config.get_field).lgtm_listen_host`; the loopback default reproduces the
# pre-parameterization output byte for byte (the contract lock for the MEIVA_LGTM_LISTEN_HOST
# knob).
def test_native_backend_listen_hosts_are_settings_rendered_with_loopback_defaults(
    tmp_path: Path,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_machine: MachineIdentityReader,
) -> None:
    native = Path(__file__).resolve().parents[3] / "deploy/lgtm/native"
    loki = (native / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: __LGTM_LISTEN_HOST__" in loki
    assert "grpc_listen_address: __LGTM_LISTEN_HOST__" in loki
    # Single-binary internal addresses stay pinned to loopback by design.
    assert "instance_addr: 127.0.0.1" in loki
    assert "address: 127.0.0.1" in loki
    assert (
        "--web.listen-address={lgtm_listen_host}:{lgtm_prometheus_port}"
        in lgtm_native.NATIVE_CONSTANTS["prometheus"].arguments
    )

    repo = Path(__file__).resolve().parents[3]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"
    _render_configs(
        repo,
        native_dir,
        home,
        settings=lgtm_settings(observability=observability(lgtm_listen_host="127.0.0.1")),
        producer=operator_pipeline,
        machine=command_machine,
    )
    rendered_loki = (native_dir / "config/loki.yaml").read_text(encoding="utf-8")
    assert "http_listen_address: 127.0.0.1" in rendered_loki
    assert "grpc_listen_address: 127.0.0.1" in rendered_loki
    # meiva:note
    # The Prometheus unit's argv is `base.telemetry.lgtm_local.service_argv`, which reads the
    # process-wide listen host instead of taking it as a value; it is pinned here for the argv only.
    prior_host = config.get_field("lgtm_listen_host")
    try:
        config.set_field("lgtm_listen_host", "127.0.0.1")
        prometheus_argv = service_argv(
            home, "prometheus", LocalLgtmSettings.from_fields(config.get_field)
        )
        assert "--web.listen-address=127.0.0.1:9090" in prometheus_argv

        # A non-loopback setting flows through to the rendered listeners.
        _render_configs(
            repo,
            native_dir,
            home,
            settings=lgtm_settings(observability=observability(lgtm_listen_host="10.0.0.5")),
            producer=operator_pipeline,
            machine=command_machine,
        )
        rendered_loki = (native_dir / "config/loki.yaml").read_text(encoding="utf-8")
        assert "http_listen_address: 10.0.0.5" in rendered_loki
        assert "grpc_listen_address: 10.0.0.5" in rendered_loki
        config.set_field("lgtm_listen_host", "10.0.0.5")
        prometheus_argv = service_argv(
            home, "prometheus", LocalLgtmSettings.from_fields(config.get_field)
        )
        assert "--web.listen-address=10.0.0.5:9090" in prometheus_argv
    finally:
        config.set_field("lgtm_listen_host", prior_host)


def _assert_rendered_provisioning(
    native_dir: Path,
    *,
    loki: str | None = None,
    prometheus: str | None = None,
    pg: str | None = None,
    webhook: str | None = None,
) -> None:
    """Parse the converge-rendered provisioning tree and lock the datasource
    and webhook URL values (default rendering output contract)."""
    rendered_datasources = yaml.safe_load(
        (native_dir / "config/provisioning/datasources/datasources.yml").read_text(encoding="utf-8")
    )
    by_uid = {ds["uid"]: ds["url"] for ds in rendered_datasources["datasources"]}
    if loki is not None:
        assert by_uid["loki"] == loki
    if prometheus is not None:
        assert by_uid["prometheus"] == prometheus
    if pg is not None:
        assert by_uid["ops"] == pg
    datasources_text = (native_dir / "config/provisioning/datasources/datasources.yml").read_text(
        encoding="utf-8"
    )
    assert "{{" not in datasources_text
    rendered_contact = yaml.safe_load(
        (native_dir / "config/provisioning/alerting/contact.yml").read_text(encoding="utf-8")
    )
    if webhook is not None:
        assert rendered_contact["contactPoints"][0]["receivers"][0]["settings"]["url"] == webhook
    contact_text = (native_dir / "config/provisioning/alerting/contact.yml").read_text(
        encoding="utf-8"
    )
    assert "{{" not in contact_text


def _assert_contains(text: str, *needles: str) -> None:
    for needle in needles:
        assert needle in text


def test_native_grafana_renders_from_the_repo_and_host_setting(
    tmp_path: Path,
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_machine: MachineIdentityReader,
) -> None:
    repo = Path(__file__).resolve().parents[3]
    home = tmp_path / "home"
    native_dir = home / "lgtm/native"

    _render_configs(
        repo,
        native_dir,
        home,
        settings=lgtm_settings(
            observability=observability(
                telemetry_tempo_query_url="http://tempo.test:3200/",
                telemetry_tempo_endpoint="http://tempo.test:14318/",
            )
        ),
        producer=operator_pipeline,
        machine=command_machine,
    )
    grafana_ini = (native_dir / "config/grafana.ini").read_text(encoding="utf-8")
    runtime_env = (native_dir / "config/runtime.env").read_text(encoding="utf-8")
    run_script = (native_dir / "grafana/run.sh").read_text(encoding="utf-8")
    prometheus = yaml.safe_load((native_dir / "config/prometheus.yml").read_text(encoding="utf-8"))

    for text in (grafana_ini, runtime_env, run_script):
        assert "{{" not in text
    _assert_contains(
        runtime_env,
        f"GRAFANA_PROVISIONING_PATH={native_dir}/config/provisioning/dashboards",
        "MEIVA_TELEMETRY_TEMPO_QUERY_URL=http://tempo.test:3200",
    )
    _assert_contains(
        run_script,
        "admin_password",
        'export GRAFANA_ROOT_URL="${GRAFANA_ROOT_URL:-http://localhost:3003}"',
        f"{repo}/deploy/lgtm/.env",
        f"{native_dir}/config/runtime.env",
        str(native_dir / "grafana-home/bin/grafana"),
        str(native_dir / "config/grafana.ini"),
        str(native_dir / "grafana-home"),
    )
    assert {
        job["job_name"]: job["static_configs"][0]["targets"] for job in prometheus["scrape_configs"]
    }["tempo"] == ["tempo.test:3200"]

    # The repo datasources.yml is a template; the rendered copy under the
    # native dir carries the baked URLs (default = loopback, byte-identical
    # to the pre-parameterization content).
    template = (
        repo / "deploy/lgtm/config/grafana/provisioning/datasources/datasources.yml"
    ).read_text(encoding="utf-8")
    _assert_contains(
        template,
        "$__env{MEIVA_TELEMETRY_LOKI_URL}",
        "$__env{MEIVA_TELEMETRY_PROMETHEUS_URL}",
        "$__env{MEIVA_PG_URL}",
        "$__env{MEIVA_TELEMETRY_TEMPO_QUERY_URL}",
    )
    # The rendered provisioning tree keeps the $__env{} references verbatim;
    # the two-state VALUES are baked into runtime.env (Grafana expands at
    # runtime from its process env).
    rendered_datasources = (
        native_dir / "config/provisioning/datasources/datasources.yml"
    ).read_text(encoding="utf-8")
    assert "$__env{MEIVA_TELEMETRY_LOKI_URL}" in rendered_datasources
    for text in (template, rendered_datasources):
        assert "{{" not in text
    rendered_runtime_env = (native_dir / "config/runtime.env").read_text(encoding="utf-8")
    _assert_contains(
        rendered_runtime_env,
        "MEIVA_TELEMETRY_LOKI_URL=http://127.0.0.1:3100",
        "MEIVA_TELEMETRY_PROMETHEUS_URL=http://127.0.0.1:9090",
        "MEIVA_PG_URL=127.0.0.1:5433",
        "MEIVA_ALERTS_WEBHOOK_URL=http://127.0.0.1:8000/api/alerts",
    )
    _assert_rendered_provisioning(native_dir, loki="$__env{MEIVA_TELEMETRY_LOKI_URL}")


def test_native_loki_template_ships_the_noise_reducing_log_level() -> None:
    repo = Path(__file__).resolve().parents[3]
    native = yaml.safe_load(
        (repo / "deploy/lgtm/native/config/loki.yaml").read_text(encoding="utf-8")
    )
    # The default info level writes every flush stream per chunk into the service log (task #1978).
    assert native["server"]["log_level"] == "warn"
