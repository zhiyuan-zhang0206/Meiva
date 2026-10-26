# meiva:note
# The converge shell around the collector sidecar, over real files: the guarded config write, the
# install pass, the step that gates it, and the shipped template rendered for each role shape.
# What the config says and whether to install are decided by the modules under `plans/`
# (`test_collector_plans.py`); these tests prove the shell hands them the right values and that
# their output fits the real template. The remote ingress topologies live in
# `converge_otel_collector/`.
# meiva:link cli/commands/observability/docs/operations.md

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest
import yaml

from base.cluster.authority import MONITOR_ROLE
from base.cluster.dataplane.pg_admin import pg_socket_path
from base.deploy.release import collector_artifact
from base.host.db_authority.unit import telemetry_bearer
from base.host.env.dotenv_boot import PLACEHOLDER_DB_URL
from base.host.system.tests.fake_commands import fake_host
from base.native_process.os_platform import Platform
from base.paths import otel_collector_config, otel_collector_dir
from cli.commands.observability import otel_collector as oc
from cli.commands.observability.inputs import collector_settings_from_boot
from cli.commands.observability.plans.collector.data_plane import PEER_PLACEHOLDER
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    GATEWAY,
    GATEWAY_INGRESS,
    HYBRID,
    RUNNER,
    STATION,
    CollectorUnit,
)
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    collector_unit as collector_unit,
)
from cli.commands.observability.tests.observability_inputs import (
    data_plane,
    observability,
    write_cluster_record,
)
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime

POSTGRES_PORT = 5433
DELIVERED_LOGIN = "postgresql://meiva_g3_gateway:generation-login-password@127.0.0.1:1/meiva"


def _rotation(config: dict[str, Any]) -> dict[str, Any]:
    return config["exporters"]["file/traces"]["rotation"]


def _infra_receivers(config: dict[str, Any]) -> list[str]:
    return config["service"]["pipelines"]["metrics/infra"]["receivers"]


def test_the_collector_settings_are_read_from_the_domains_of_one_owner(
    command_runtime: CommandRuntime,
) -> None:
    view = command_runtime.config.view

    values = collector_settings_from_boot(command_runtime.config)

    for group, domain in (
        (values.observability, view.observability),
        (values.data_plane, view.data_plane),
    ):
        assert dataclasses.asdict(group) == {
            field.name: getattr(domain, field.name) for field in dataclasses.fields(group)
        }
    limits = view.observability
    assert values.download == collector_artifact.DownloadLimits(
        total_budget_s=limits.otel_collector_download_budget_s,
        socket_timeout_s=limits.otel_collector_download_socket_timeout_s,
        attempts=limits.otel_collector_download_attempts,
        retry_backoff_s=limits.otel_collector_download_retry_backoff_s,
        progress_interval_s=limits.otel_collector_download_progress_interval_s,
    )


def test_a_hand_edited_config_survives_the_next_converge_with_a_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "otel-collector" / "config.yaml"
    config.parent.mkdir()
    oc.write_config(config, "otelcol: default\n")
    assert config.read_text(encoding="utf-8") == "otelcol: default\n"

    config.write_text("otelcol: user-edit\n", encoding="utf-8")
    oc.write_config(config, "otelcol: regenerated\n")

    assert config.read_text(encoding="utf-8") == "otelcol: user-edit\n"
    assert "modified locally" in capsys.readouterr().err


def test_a_config_the_converge_wrote_is_replaced_by_the_next_render(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "otel-collector" / "config.yaml"
    config.parent.mkdir()
    oc.write_config(config, "v1\n")
    oc.write_config(config, "v2\n")

    assert config.read_text(encoding="utf-8") == "v2\n"
    assert capsys.readouterr().err == ""


def test_a_version_matching_binary_is_kept_and_the_config_is_written_owner_only(
    collector_unit: CollectorUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    binary = collector_unit.install_pinned_binary()

    collector_unit.ensure(
        RUNNER, observability=observability(gateway_otlp_endpoint=GATEWAY_INGRESS)
    )

    assert binary.read_bytes() == b"bin"
    assert (
        f"otelcol-contrib {collector_artifact.OTELCOL_CONTRIB_VERSION} present"
        in capsys.readouterr().out
    )
    config = otel_collector_config(collector_unit.home)
    bearer = telemetry_bearer(collector_unit.home.resolve(), "cluster-token")
    assert bearer is not None
    assert config.stat().st_mode & 0o777 == 0o600
    text = config.read_text(encoding="utf-8")
    assert f"Bearer {bearer}" in text
    assert "cluster-token" not in text


def test_changed_settings_reach_the_config_on_the_next_converge_but_a_hand_edit_does_not(
    collector_unit: CollectorUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    collector_unit.install_pinned_binary()
    config = otel_collector_config(collector_unit.home)

    collector_unit.ensure(None, observability=observability(trace_retention_days=3))
    assert _rotation(yaml.safe_load(config.read_text(encoding="utf-8")))["max_days"] == 3
    collector_unit.ensure(None, observability=observability(trace_retention_days=9))
    assert _rotation(yaml.safe_load(config.read_text(encoding="utf-8")))["max_days"] == 9

    config.write_text("user: edit\n", encoding="utf-8")
    collector_unit.ensure(None, observability=observability(trace_retention_days=12))

    assert config.read_text(encoding="utf-8") == "user: edit\n"
    assert "modified locally" in capsys.readouterr().err


def test_a_platform_without_a_pinned_collector_warns_and_installs_nothing(
    collector_unit: CollectorUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    collector_unit.ensure(None, tag=None, platform_label="Linux i686")

    captured = capsys.readouterr()
    assert "no pinned otelcol-contrib for this platform (Linux i686)" in captured.err
    assert captured.out == ""
    assert not otel_collector_dir(collector_unit.home).exists()


# meiva:note
# The converge step takes the asset tag from the platform of the host it was handed, not from the
# process running the test: with the pinned binary already installed, a platform the release has
# an asset for installs nothing and says nothing, and one it has none for warns with that
# platform's label.
@pytest.mark.parametrize(
    ("machine", "warned"), [("aarch64", False), ("arm64", False), ("riscv64", True)]
)
def test_the_step_takes_the_asset_tag_from_the_hosts_platform(
    collector_unit: CollectorUnit,
    capsys: pytest.CaptureFixture[str],
    machine: str,
    warned: bool,
) -> None:
    collector_unit.install_pinned_binary()
    ctx = dataclasses.replace(
        collector_unit.converge_ctx(None),
        host=fake_host(platform=Platform("linux", machine, "/usr/bin/python3")),
    )

    oc.ensure_otel_collector_step(ctx)

    err = capsys.readouterr().err
    assert ("no pinned otelcol-contrib for this platform (linux " + machine + ")" in err) is warned


@pytest.mark.parametrize(
    "roles", [pytest.param(GATEWAY, id="gateway"), pytest.param(HYBRID, id="hybrid")]
)
def test_a_gateway_home_that_is_not_the_station_skips_the_collector_and_says_why(
    collector_unit: CollectorUnit, capsys: pytest.CaptureFixture[str], roles: frozenset[str]
) -> None:
    collector_unit.machine.set(role=roles)

    oc.ensure_otel_collector_step(collector_unit.converge_ctx(roles))

    err = capsys.readouterr().err
    assert "collector skipped" in err
    assert "lgtm-host" in err
    assert not otel_collector_dir(collector_unit.home).exists()


# meiva:note
# The operator's explicit `MEIVA_TELEMETRY_OTLP_ENDPOINT` is the escape hatch that keeps a
# non-station gateway's collector: the step reads it from the launch environment its configuration
# owner holds, and prepares the collector instead of skipping it.
def test_a_gateway_home_with_an_explicit_collector_endpoint_prepares_the_collector(
    collector_unit: CollectorUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    collector_unit.machine.set(role=GATEWAY)
    collector_unit.runtime.config.set_field("db_url", PLACEHOLDER_DB_URL)
    collector_unit.runtime.config.environment = {
        "MEIVA_TELEMETRY_OTLP_ENDPOINT": "http://collector.invalid:4318"
    }
    collector_unit.install_pinned_binary()

    oc.ensure_otel_collector_step(collector_unit.converge_ctx(GATEWAY))

    assert "collector skipped" not in capsys.readouterr().err
    assert otel_collector_config(collector_unit.home).is_file()


def test_a_residual_config_is_preserved_and_reported_when_the_collector_is_skipped(
    collector_unit: CollectorUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    collector_unit.machine.set(role=GATEWAY)
    config = otel_collector_config(collector_unit.home)
    config.parent.mkdir(parents=True)
    config.write_text("stale: true\n", encoding="utf-8")

    oc.ensure_otel_collector_step(collector_unit.converge_ctx(GATEWAY))

    assert config.read_text(encoding="utf-8") == "stale: true\n"
    assert "stale/residual" in capsys.readouterr().err


def test_the_station_gateway_home_renders_its_collector_from_the_converge_context(
    collector_unit: CollectorUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    collector_unit.machine.set(role=GATEWAY)
    (collector_unit.home / "lgtm-host").write_text("", encoding="utf-8")
    collector_unit.runtime.config.set_field("db_url", PLACEHOLDER_DB_URL)
    collector_unit.runtime.config.set_field("trace_retention_days", 5)
    binary = collector_unit.install_pinned_binary()

    oc.ensure_otel_collector_step(collector_unit.converge_ctx(GATEWAY))

    assert binary.read_bytes() == b"bin"
    config = otel_collector_config(collector_unit.home)
    assert config.stat().st_mode & 0o777 == 0o600
    assert _rotation(yaml.safe_load(config.read_text(encoding="utf-8")))["max_days"] == 5
    assert "collector skipped" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "roles",
    [
        pytest.param(None, id="unconfigured"),
        pytest.param(GATEWAY, id="gateway"),
        pytest.param(RUNNER, id="runner"),
        pytest.param(STATION, id="station"),
        pytest.param(HYBRID, id="hybrid"),
    ],
)
def test_the_shipped_template_is_fully_consumed_for_every_role_shape(
    collector_unit: CollectorUnit, roles: frozenset[str] | None
) -> None:
    config = collector_unit.render(
        roles, observability=observability(gateway_otlp_endpoint=GATEWAY_INGRESS)
    )

    assert set(config) == {"receivers", "processors", "extensions", "exporters", "service"}


def test_the_observability_values_are_baked_into_the_fan_out_and_the_retention(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(
        None,
        observability=observability(
            telemetry_tempo_endpoint="http://10.0.0.2:14318",
            telemetry_loki_url="http://10.0.0.2:3100",
            telemetry_prometheus_url="http://10.0.0.2:9090",
            trace_retention_days=7,
        ),
    )

    endpoints = {
        name: body["endpoint"]
        for name, body in config["exporters"].items()
        if name.startswith("otlphttp/")
    }
    assert endpoints == {
        "otlphttp/tempo": "http://10.0.0.2:14318",
        "otlphttp/loki": "http://10.0.0.2:3100/otlp",
        "otlphttp/prometheus": "http://10.0.0.2:9090/api/v1/otlp",
    }
    assert _rotation(config)["max_days"] == 7
    assert config["exporters"]["file/traces"]["path"] == f"{collector_unit.home}/traces/spans.jsonl"


def test_the_trace_mirror_rotates_by_size_with_bounded_backups(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(HYBRID)

    assert _rotation(config) == {"max_megabytes": 64, "max_backups": 24, "max_days": 3}


@pytest.mark.parametrize(
    "roles",
    [
        pytest.param(GATEWAY, id="gateway"),
        pytest.param(RUNNER, id="runner"),
        pytest.param(STATION, id="station"),
    ],
)
def test_every_hop_drops_the_newest_batch_after_a_bounded_retry_and_queues_in_memory(
    collector_unit: CollectorUnit, roles: frozenset[str]
) -> None:
    config = collector_unit.render(
        roles, observability=observability(gateway_otlp_endpoint=GATEWAY_INGRESS)
    )
    exporters = config["exporters"]

    for exporter_id, queue_size in (
        ("otlphttp/tempo", 5000),
        ("otlphttp/loki", 5000),
        ("otlphttp/prometheus", 1000),
    ):
        assert exporters[exporter_id]["timeout"] == "5s"
        assert exporters[exporter_id]["sending_queue"] == {
            "enabled": True,
            "queue_size": queue_size,
            "block_on_overflow": False,
            "wait_for_result": False,
        }
        assert exporters[exporter_id]["retry_on_failure"] == {
            "enabled": True,
            "initial_interval": "5s",
            "max_interval": "30s",
            "max_elapsed_time": "15m",
        }
    assert "file_storage" not in config["extensions"]
    assert "file_storage" not in config["service"]["extensions"]
    assert "file_storage/logoffsets" in config["extensions"]
    assert config["service"]["pipelines"]["traces"]["exporters"] == [
        "otlphttp/tempo",
        "file/traces",
    ]


def test_a_gateway_scrapes_its_own_data_plane_without_the_write_generations_login(
    collector_unit: CollectorUnit,
) -> None:
    text = collector_unit.text(HYBRID, data_plane=data_plane(db_url=DELIVERED_LOGIN))
    config = yaml.safe_load(text)

    assert "generation-login-password" not in text
    assert "meiva_g3_gateway" not in text
    receivers = config["receivers"]
    socket_dir = pg_socket_path(collector_unit.home).as_posix()
    assert receivers["postgresql"] == {
        "endpoint": f"{socket_dir.lstrip('/')}:{POSTGRES_PORT}",
        "transport": "unix",
        "username": MONITOR_ROLE,
        "password": PEER_PLACEHOLDER,
        "databases": ["meiva"],
        "collection_interval": "60s",
        "tls": {"insecure": True},
    }
    assert receivers["redis"]["endpoint"] == "127.0.0.1:6380"
    assert receivers["redis"]["password"] == data_plane().redis_admin_password
    assert receivers["redis"]["password"] != data_plane().cluster_secret
    assert _infra_receivers(config) == ["host_metrics", "prometheus/otelcol", "postgresql", "redis"]


def test_disabling_otlp_export_keeps_the_redis_receiver_but_drops_the_postgres_one(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(
        HYBRID, observability=observability(telemetry_otlp_enabled=False)
    )

    assert "postgresql" not in config["receivers"]
    assert "redis" in config["receivers"]
    assert _infra_receivers(config) == ["host_metrics", "prometheus/otelcol", "redis"]


def test_a_remote_managed_plane_is_scraped_for_redis_only_and_leaks_no_provider_credential(
    collector_unit: CollectorUnit,
) -> None:
    text = collector_unit.text(
        HYBRID,
        host="localhost",
        data_plane=data_plane(
            db_url="postgresql://meiva:provider-password@10.0.0.2:5433/meiva",
            redis_url="redis://10.0.0.2:6380/0",
            is_remote=True,
            cluster_secret="",
        ),
    )
    config = yaml.safe_load(text)

    assert "provider-password" not in text
    assert "postgresql" not in config["receivers"]
    assert config["receivers"]["redis"]["endpoint"] == "10.0.0.2:6380"
    assert _infra_receivers(config) == ["host_metrics", "prometheus/otelcol", "redis"]


def test_a_gateway_without_a_registry_record_in_its_home_cannot_render_the_postgres_receiver(
    collector_unit: CollectorUnit, tmp_path: Path
) -> None:
    bare = dataclasses.replace(collector_unit, home=tmp_path / ".meiva-bare")

    with pytest.raises(RuntimeError, match="no registry record"):
        bare.render(HYBRID)

    write_cluster_record(bare.home)
    assert "postgresql" in bare.render(HYBRID)["receivers"]


@pytest.mark.parametrize(
    "roles",
    [
        pytest.param(None, id="unconfigured"),
        pytest.param(RUNNER, id="runner"),
        pytest.param(STATION, id="station"),
    ],
)
def test_a_unit_that_does_not_own_the_data_plane_scrapes_only_its_host(
    collector_unit: CollectorUnit, roles: frozenset[str] | None
) -> None:
    config = collector_unit.render(
        roles, observability=observability(gateway_otlp_endpoint=GATEWAY_INGRESS)
    )

    assert "postgresql" not in config["receivers"]
    assert "redis" not in config["receivers"]
    assert "host_metrics" in config["receivers"]
    assert _infra_receivers(config) == ["host_metrics", "prometheus/otelcol"]


def test_the_host_identity_processors_sit_on_the_infra_pipeline_only(
    collector_unit: CollectorUnit,
) -> None:
    pipelines = collector_unit.render(GATEWAY)["service"]["pipelines"]

    assert pipelines["metrics"]["receivers"] == ["otlp", "otlp/remote"]
    assert "transform/host_label" not in pipelines["metrics"]["processors"]
    assert "transform/host_label" in pipelines["metrics/infra"]["processors"]
    assert "resource_detection/host" in pipelines["metrics/infra"]["processors"]


def test_infra_datapoints_keep_the_physical_host_and_gain_the_roster_machine_name(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(GATEWAY)

    statements = config["processors"]["transform/host_label"]["metric_statements"][0]["statements"]
    assert 'set(attributes["host"], resource.attributes["host.name"])' in statements
    assert 'set(attributes["machine_name"], "test-machine")' in statements


@pytest.mark.parametrize("port", [8888, 8889])
def test_the_collector_binds_and_scrapes_its_own_metrics_port(
    collector_unit: CollectorUnit, port: int
) -> None:
    config = collector_unit.render(
        RUNNER,
        observability=observability(
            gateway_otlp_endpoint=GATEWAY_INGRESS, otel_collector_metrics_port=port
        ),
    )

    reader = config["service"]["telemetry"]["metrics"]["readers"][0]["pull"]["exporter"]
    assert reader["prometheus"] == {"host": "localhost", "port": port}
    assert config["receivers"]["prometheus/otelcol"]["config"]["scrape_configs"] == [
        {
            "job_name": "meiva-otel-collector",
            "scrape_interval": "30s",
            "static_configs": [{"targets": [f"localhost:{port}"]}],
        }
    ]


def test_the_collectors_internal_logs_are_warn_level(collector_unit: CollectorUnit) -> None:
    config = collector_unit.render(GATEWAY)

    assert config["service"]["telemetry"]["logs"]["level"] == "warn"


def test_logs_promote_event_labels_and_label_tailed_files_before_the_final_batch(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(HYBRID)

    promote = config["processors"]["transform/promote_event_labels"]
    assert promote["error_mode"] == "ignore"
    assert promote["log_statements"] == [
        {
            "context": "log",
            "statements": [
                'set(resource.attributes["agent_id"], attributes["agent_id"]) where attributes["agent_id"] != nil',
                'set(resource.attributes["event_name"], attributes["event_name"]) where attributes["event_name"] != nil',
            ],
        }
    ]
    assert config["processors"]["transform/filelog_service"]["log_statements"] == [
        {
            "context": "log",
            "conditions": ['attributes["log.file.name"] != nil'],
            "statements": [
                'set(attributes["tmp_svc"], attributes["log.file.name"])',
                'replace_pattern(attributes["tmp_svc"], "\\\\.out\\\\.log$", "")',
                'set(resource.attributes["service.name"], attributes["tmp_svc"])',
                'delete_key(attributes, "tmp_svc")',
            ],
        }
    ]
    assert config["extensions"]["file_storage/logoffsets"] == {
        "directory": f"{collector_unit.home}/otel-collector/log-offsets",
        "create_directory": True,
    }
    logs = config["service"]["pipelines"]["logs"]
    assert logs["receivers"] == ["otlp", "otlp/remote", "filelog/sessions", "filelog/services"]
    assert logs["processors"] == [
        "memory_limiter",
        "filter/cluster_allow",
        "transform/promote_event_labels",
        "transform/filelog_service",
        "batch",
    ]


def test_local_otlp_pipelines_drop_resources_that_name_another_cluster(
    collector_unit: CollectorUnit,
) -> None:
    config = collector_unit.render(GATEWAY)

    expected = [
        'resource.attributes["cluster"] != nil and resource.attributes["cluster"] != ".meiva"'
    ]
    assert config["processors"]["filter/cluster_allow"] == {
        "error_mode": "ignore",
        "traces": {"span": expected},
        "metrics": {"metric": expected},
        "logs": {"log_record": expected},
    }
    pipelines = config["service"]["pipelines"]
    for name in ("logs", "metrics", "traces"):
        assert pipelines[name]["processors"][:2] == ["memory_limiter", "filter/cluster_allow"]
    assert "filter/cluster_allow" not in pipelines["metrics/infra"]["processors"]
    assert "filter/cluster_allow" not in pipelines["traces/remote"]["processors"]
