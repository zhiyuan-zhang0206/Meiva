# meiva:note
# The native LGTM converge pass end to end over real files, real child processes and a real
# release mirror on port 0.
#
# `ensure_lgtm_native` reads the pins from the checkout, downloads what the install plan names
# from the mirror, verifies and extracts the archives, renders the tree, runs the pinned Loki
# parser (a script the test installs, so it really runs) and writes the credentials. The plan
# that decides which backends need an archive is tested in `plans/`; these cases prove the shell
# executes it. The database is the throwaway Postgres the fixtures provision.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from base.agents.context.clients import DatabaseFactory
from base.host.system.tests.fake_commands import DARWIN_ARM64, LINUX_X86
from base.native_process.os_platform import Platform
from base.telemetry import DrainStatus, Event, EventPipeline
from base.telemetry.loki_index_labels import validate_loki_deploy_config
from cli.commands.observability import _lgtm_provisioning, lgtm_native
from cli.commands.observability.inputs import LgtmSettings
from cli.commands.observability.plans.lgtm.dashboard_plan import (
    FailedDashboardRender,
    RenderedDashboard,
)
from cli.commands.observability.plans.observability_config import GrafanaCredentials
from cli.commands.observability.tests.lgtm.lgtm_harness import (
    STUB_DASHBOARD,
    LgtmUnit,
    Mirror,
    checkout_pinning,
    host_on,
    lgtm_unit,
)
from cli.commands.observability.tests.lgtm.lgtm_harness import (
    operator_pipeline as operator_pipeline,
)
from cli.commands.observability.tests.observability_inputs import (
    REPO_ROOT,
    lgtm_settings,
    observability,
)
from cli.commands.observability.tests.scripted_http import serve
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime

__all__ = ["lgtm_unit"]

ALL_BACKENDS = frozenset({"loki", "prometheus", "grafana"})
DASHBOARD_REL = "config/provisioning/dashboards/meiva-ops-main.json"


def install_loki_stub(unit: LgtmUnit, *, exit_code: int = 0) -> None:
    # meiva:note
    # The pinned Loki binary stands in as a shell script: converge runs it with `-verify-config`
    # for real, and the exit code and stderr it answers with are what the parser would say.
    binary = unit.native / "bin" / "loki"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(
        f"#!/bin/sh\necho 'unknown config field' >&2\nexit {exit_code}\n", encoding="utf-8"
    )
    binary.chmod(0o755)


def ensure(
    unit: LgtmUnit,
    repo: Path,
    *,
    host_platform: Platform = DARWIN_ARM64,
    services: frozenset[str] = ALL_BACKENDS,
    settings: LgtmSettings | None = None,
    storage_dir: Path | None = None,
    database: DatabaseFactory,
) -> None:
    lgtm_native.ensure_lgtm_native(
        repo,
        unit.home,
        services=services,
        settings=settings or lgtm_settings(),
        storage_dir=storage_dir or unit.native / "data",
        database_factory=database,
        producer=unit.producer,
        machine=unit.machine,
        host=host_on(host_platform),
    )


def assert_installed(native: Path, *, tag: str) -> None:
    # meiva:note
    # What a finished pass leaves: the executables, a version and a platform marker per backend,
    # the rendered configs, the launcher and the retained data directories.
    assert (native / "bin/prometheus").stat().st_mode & 0o777 == 0o755
    assert (native / "grafana-home/bin/grafana").is_file()
    for name, version in (("loki", "3.7.6"), ("prometheus", "3.13.2"), ("grafana", "13.2.3")):
        assert (native / f"version-{name}").read_text(encoding="utf-8") == version + "\n"
        assert (native / f"platform-{name}").read_text(encoding="utf-8") == tag + "\n"
    for name in ("loki.yaml", "prometheus.yml", "grafana.ini", "runtime.env"):
        assert (native / "config" / name).is_file()
    assert (native / "grafana/run.sh").stat().st_mode & 0o777 == 0o755
    assert (native / "data/loki").is_dir()
    assert (native / "data/prom").is_dir()


def test_a_clean_station_downloads_verifies_installs_and_renders_every_backend(
    db_url: str,
    lgtm_unit: LgtmUnit,
    operator_database: DatabaseFactory,
    capsys: pytest.CaptureFixture[str],
) -> None:
    mirror = Mirror("linux-amd64")
    with serve(mirror.routes()) as served:
        repo = checkout_pinning(lgtm_unit.home / "repo", mirror.pins(served.url))
        ensure(lgtm_unit, repo, host_platform=LINUX_X86, database=operator_database)

    assert (lgtm_unit.native / "bin/loki").read_bytes() == mirror.loki_script()
    assert_installed(lgtm_unit.native, tag="linux_amd64")
    assert sorted(request.path for request in served.received) == [
        "/grafana.tar.gz",
        "/loki.zip",
        "/prometheus.tar.gz",
    ]
    out = capsys.readouterr().out
    assert "installed loki 3.7.6 (linux_amd64)" in out
    assert "installed grafana 13.2.3 (linux_amd64)" in out


def test_backends_already_installed_for_this_platform_are_not_downloaded(
    db_url: str, lgtm_unit: LgtmUnit, operator_database: DatabaseFactory
) -> None:
    mirror = Mirror("darwin-arm64")
    lgtm_unit.mark_current("darwin", "arm64")
    install_loki_stub(lgtm_unit)
    with serve({}) as served:
        repo = checkout_pinning(lgtm_unit.home / "repo", mirror.pins(served.url))
        ensure(lgtm_unit, repo, database=operator_database)

    assert served.received == []
    assert (lgtm_unit.native / "config/loki.yaml").exists()


def test_only_the_backend_with_a_stale_marker_is_downloaded_again(
    db_url: str, lgtm_unit: LgtmUnit, operator_database: DatabaseFactory
) -> None:
    mirror = Mirror("darwin-arm64")
    lgtm_unit.mark_current("darwin", "arm64")
    (lgtm_unit.native / "version-loki").write_text("old\n", encoding="utf-8")
    with serve(mirror.routes("/loki.zip")) as served:
        repo = checkout_pinning(lgtm_unit.home / "repo", mirror.pins(served.url))
        ensure(lgtm_unit, repo, database=operator_database)

    assert [request.path for request in served.received] == ["/loki.zip"]
    assert (lgtm_unit.native / "version-loki").read_text(encoding="utf-8") == "3.7.6\n"


def test_versions_installed_for_another_platform_are_downloaded_again(
    db_url: str, lgtm_unit: LgtmUnit, operator_database: DatabaseFactory
) -> None:
    mirror = Mirror("linux-amd64")
    lgtm_unit.mark_current("darwin", "arm64")
    with serve(mirror.routes()) as served:
        repo = checkout_pinning(lgtm_unit.home / "repo", mirror.pins(served.url))
        ensure(lgtm_unit, repo, host_platform=LINUX_X86, database=operator_database)

    assert len(served.received) == 3
    assert (lgtm_unit.native / "platform-loki").read_text(encoding="utf-8") == "linux_amd64\n"


def test_only_the_selected_backend_is_downloaded_and_its_siblings_are_left_alone(
    db_url: str, lgtm_unit: LgtmUnit, operator_database: DatabaseFactory
) -> None:
    mirror = Mirror("darwin-arm64")
    with serve(mirror.routes("/prometheus.tar.gz")) as served:
        repo = checkout_pinning(lgtm_unit.home / "repo", mirror.pins(served.url))
        ensure(
            lgtm_unit,
            repo,
            services=frozenset({"prometheus"}),
            settings=lgtm_settings(
                credentials=GrafanaCredentials("never-rendered", "bot-token", 7)
            ),
            database=operator_database,
        )

    assert [request.path for request in served.received] == ["/prometheus.tar.gz"]
    assert (lgtm_unit.native / "bin/prometheus").is_file()
    assert not (lgtm_unit.native / "bin/loki").exists()
    assert not (lgtm_unit.native / "grafana/admin_password").exists()
    assert not (lgtm_unit.native / "grafana/telegram.env").exists()


def test_a_selection_naming_no_backend_or_a_foreign_service_is_refused(
    lgtm_unit: LgtmUnit, operator_database: DatabaseFactory
) -> None:
    for services in (frozenset[str](), frozenset({"tempo"})):
        with pytest.raises(ValueError, match="selected backend names"):
            ensure(lgtm_unit, REPO_ROOT, services=services, database=operator_database)

    assert not lgtm_unit.home.exists()


def test_a_platform_without_pinned_binaries_is_refused_before_anything_is_written(
    lgtm_unit: LgtmUnit, operator_database: DatabaseFactory
) -> None:
    with pytest.raises(RuntimeError, match="no pinned binaries for this platform"):
        ensure(
            lgtm_unit,
            REPO_ROOT,
            host_platform=Platform("linux", "aarch64", "/usr/bin/python3"),
            database=operator_database,
        )

    assert not lgtm_unit.home.exists()


def test_a_grafana_password_and_a_telegram_bot_are_written_owner_only_with_grafana(
    db_url: str, lgtm_unit: LgtmUnit, operator_database: DatabaseFactory
) -> None:
    lgtm_unit.mark_current("darwin", "arm64")
    install_loki_stub(lgtm_unit)

    ensure(
        lgtm_unit,
        REPO_ROOT,
        settings=lgtm_settings(
            credentials=GrafanaCredentials("fake-key-for-test", "fake-bot-token-for-test", 42)
        ),
        database=operator_database,
    )

    credential = lgtm_unit.native / "grafana/admin_password"
    assert credential.read_text(encoding="utf-8") == "fake-key-for-test\n"
    assert credential.stat().st_mode & 0o777 == 0o600
    env_file = lgtm_unit.native / "grafana/telegram.env"
    assert "TELEGRAM_CHAT_ID=42" in env_file.read_text(encoding="utf-8")
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_a_later_pass_without_a_bot_removes_its_credential_file(
    db_url: str, lgtm_unit: LgtmUnit, operator_database: DatabaseFactory
) -> None:
    lgtm_unit.mark_current("darwin", "arm64")
    install_loki_stub(lgtm_unit)
    with_bot = lgtm_settings(credentials=GrafanaCredentials(None, "fake-bot-token-for-test", 42))
    ensure(lgtm_unit, REPO_ROOT, settings=with_bot, database=operator_database)
    assert (lgtm_unit.native / "grafana/telegram.env").exists()

    ensure(lgtm_unit, REPO_ROOT, database=operator_database)

    assert not (lgtm_unit.native / "grafana/telegram.env").exists()
    assert not (lgtm_unit.native / "grafana/admin_password").exists()


def test_a_loki_config_the_pinned_parser_rejects_blocks_preparation(
    db_url: str, lgtm_unit: LgtmUnit, operator_database: DatabaseFactory
) -> None:
    lgtm_unit.mark_current("darwin", "arm64")
    install_loki_stub(lgtm_unit, exit_code=1)

    with pytest.raises(RuntimeError, match="Loki config verification failed: unknown config field"):
        ensure(lgtm_unit, REPO_ROOT, database=operator_database)


def test_the_configured_storage_volume_holds_the_observation_data_and_the_home_none(
    db_url: str,
    lgtm_unit: LgtmUnit,
    operator_database: DatabaseFactory,
    tmp_path: Path,
) -> None:
    lgtm_unit.mark_current("darwin", "arm64")
    install_loki_stub(lgtm_unit)
    storage = tmp_path / "obs-data"

    ensure(lgtm_unit, REPO_ROOT, storage_dir=storage, database=operator_database)

    assert (storage / "loki").is_dir()
    assert (storage / "prom").is_dir()
    assert not (lgtm_unit.native / "data").exists()
    loki = yaml.safe_load((lgtm_unit.native / "config/loki.yaml").read_text(encoding="utf-8"))
    assert loki["common"]["path_prefix"] == f"{storage}/loki"
    assert loki["compactor"]["working_directory"] == f"{storage}/loki/compactor"


def test_the_rendered_tree_has_the_fixed_layout_and_the_generated_dashboard(
    lgtm_unit: LgtmUnit,
) -> None:
    lgtm_unit.render()
    config_dir = lgtm_unit.native / "config"

    assert {path.name for path in config_dir.iterdir()} == {
        "grafana.ini",
        "loki.yaml",
        "prometheus.yml",
        "runtime.env",
        "provisioning",
        "provisioning-hashes.json",
    }
    assert (config_dir / "provisioning/datasources/datasources.yml").is_file()
    assert (config_dir / "provisioning/alerting/contact.yml").is_file()
    assert (config_dir / "provisioning/alerting/rules.yml").is_file()
    assert (lgtm_unit.native / DASHBOARD_REL).read_text(encoding="utf-8") == STUB_DASHBOARD.content


# meiva:note
# The per-PID `service.instance.id` is not an index label: its churn stays structured metadata.
def test_loki_renders_native_paths_loopback_listeners_and_the_deploy_invariants(
    lgtm_unit: LgtmUnit,
) -> None:
    lgtm_unit.render()
    loki = (lgtm_unit.native / "config/loki.yaml").read_text(encoding="utf-8")
    config = yaml.safe_load(loki)

    assert "{{MEIVA_HOME}}" not in loki
    assert config["common"]["path_prefix"] == f"{lgtm_unit.native}/data/loki"
    assert config["frontend"]["address"] == "127.0.0.1"
    labels = config["distributor"]["otlp_config"]["default_resource_attributes_as_index_labels"]
    assert {"agent_id", "event_name"} <= set(labels)
    assert "service.instance.id" not in labels
    for line in (
        "http_listen_address: 127.0.0.1",
        "grpc_listen_address: 127.0.0.1",
        "instance_addr: 127.0.0.1",
        "retention_period: 84h",
        "disk_full_threshold: 0.95",
        "max_query_series: 20000",
        "query_timeout: 50s",
        "max_entries_limit_per_query: 50001",
        "split_queries_by_interval: 24h",
    ):
        assert line in loki
    validate_loki_deploy_config(config)


def scrape_targets(unit: LgtmUnit) -> dict[str, list[str]]:
    prometheus = yaml.safe_load((unit.native / "config/prometheus.yml").read_text(encoding="utf-8"))
    assert prometheus["storage"]["tsdb"]["out_of_order_time_window"] == "6h"
    return {
        job["job_name"]: job["static_configs"][0]["targets"] for job in prometheus["scrape_configs"]
    }


def test_prometheus_scrapes_the_loopback_backends_by_default(lgtm_unit: LgtmUnit) -> None:
    lgtm_unit.render()

    assert scrape_targets(lgtm_unit) == {
        "prometheus": ["127.0.0.1:9090"],
        "tempo": ["127.0.0.1:3200"],
        "loki": ["127.0.0.1:3100"],
        "grafana": ["127.0.0.1:3003"],
    }
    prometheus = yaml.safe_load(
        (lgtm_unit.native / "config/prometheus.yml").read_text(encoding="utf-8")
    )
    grafana_job = next(j for j in prometheus["scrape_configs"] if j["job_name"] == "grafana")
    assert grafana_job["metrics_path"] == "/grafana/metrics"


def test_the_scrape_targets_follow_the_telemetry_read_urls_in_the_external_migration_form(
    lgtm_unit: LgtmUnit,
) -> None:
    lgtm_unit.render(
        settings=lgtm_settings(
            observability=observability(
                lgtm_listen_host="10.0.0.5",
                telemetry_loki_url="http://10.0.0.5:3100",
                telemetry_prometheus_url="http://10.0.0.5:9090",
                telemetry_grafana_url="http://10.0.0.5:3003",
            )
        )
    )

    assert scrape_targets(lgtm_unit) == {
        "prometheus": ["10.0.0.5:9090"],
        "tempo": ["127.0.0.1:3200"],
        "loki": ["10.0.0.5:3100"],
        "grafana": ["10.0.0.5:3003"],
    }


def test_the_prometheus_template_keeps_the_late_sample_window() -> None:
    config = yaml.safe_load(
        (REPO_ROOT / "deploy/lgtm/native/config/prometheus.yml").read_text(encoding="utf-8")
    )

    assert config["storage"]["tsdb"]["out_of_order_time_window"] == "6h"


def test_the_too_old_samples_rule_uses_the_window_delta_in_the_fast_group() -> None:
    # meiva:note
    # Focused coverage lives here because the alert-rule suite under tests/scripts is
    # structure-budget frozen.
    rules = yaml.safe_load(
        (REPO_ROOT / "deploy/lgtm/config/grafana/provisioning/alerting/rules.yml").read_text(
            encoding="utf-8"
        )
    )
    group = next(g for g in rules["groups"] if g["name"] == "meiva-ops")
    rule = next(r for r in group["rules"] if r["uid"] == "meiva-ops-prom-too-old-samples")
    expr = next(d["model"]["expr"] for d in rule["data"] if d.get("datasourceUid") == "prometheus")
    assert "prometheus_tsdb_too_old_samples_total" in expr
    assert "increase(" in expr
    assert "[10m]" in expr
    threshold = next(d for d in rule["data"] if d["model"].get("type") == "threshold")
    assert threshold["model"]["conditions"][0]["evaluator"] == {"type": "gt", "params": [0]}
    assert rule["for"] == "0m"


def provisioning_checkout(root: Path, *, with_reference: bool = True) -> Path:
    # meiva:note
    # A minimal checkout: one verbatim provisioning file next to the dashboard whose checkout
    # copy is only the render's reference output.
    source = root / "deploy/lgtm/config/grafana/provisioning"
    (source / "dashboards").mkdir(parents=True)
    (source / "datasources").mkdir()
    (source / "dashboards/dashboards.yml").write_text("yaml\n", encoding="utf-8")
    (source / "datasources/datasources.yml").write_text("datasource\n", encoding="utf-8")
    if with_reference:
        (source / "dashboards/meiva-ops-main.json").write_text('{"checkout": true}\n', "utf-8")
    return root


def test_the_dashboard_comes_from_the_render_not_from_the_checkout_copy(tmp_path: Path) -> None:
    repo = provisioning_checkout(tmp_path / "repo")
    native = tmp_path / "native"

    _lgtm_provisioning.render_provisioning(
        repo, native, dashboard=STUB_DASHBOARD, producer=_never_produced
    )

    dest_dir = native / "config/provisioning"
    assert (dest_dir / "dashboards/meiva-ops-main.json").read_text(encoding="utf-8") == (
        STUB_DASHBOARD.content
    )
    assert (dest_dir / "dashboards/dashboards.yml").read_text(encoding="utf-8") == "yaml\n"
    hashes = json.loads((native / "config/provisioning-hashes.json").read_text(encoding="utf-8"))
    assert (
        hashes["dashboards/meiva-ops-main.json"]
        == hashlib.sha256(STUB_DASHBOARD.content.encode()).hexdigest()
    )


def test_deleting_the_checkout_copy_does_not_delete_the_generated_dashboard(
    tmp_path: Path,
) -> None:
    repo = provisioning_checkout(tmp_path / "repo")
    native = tmp_path / "native"
    _lgtm_provisioning.render_provisioning(
        repo, native, dashboard=STUB_DASHBOARD, producer=_never_produced
    )
    dest = native / DASHBOARD_REL
    (repo / "deploy/lgtm/config/grafana/provisioning/dashboards/meiva-ops-main.json").unlink()

    _lgtm_provisioning.render_provisioning(
        repo, native, dashboard=STUB_DASHBOARD, producer=_never_produced
    )

    assert dest.read_text(encoding="utf-8") == STUB_DASHBOARD.content


def test_an_unchanged_render_does_not_rewrite_the_dashboard(tmp_path: Path) -> None:
    repo = provisioning_checkout(tmp_path / "repo")
    native = tmp_path / "native"
    _lgtm_provisioning.render_provisioning(
        repo, native, dashboard=STUB_DASHBOARD, producer=_never_produced
    )
    before = (native / DASHBOARD_REL).stat().st_mtime_ns

    _lgtm_provisioning.render_provisioning(
        repo, native, dashboard=STUB_DASHBOARD, producer=_never_produced
    )

    assert (native / DASHBOARD_REL).stat().st_mtime_ns == before


def test_a_dashboard_written_outside_converge_is_replaced_by_the_next_render(
    tmp_path: Path,
) -> None:
    repo = provisioning_checkout(tmp_path / "repo")
    native = tmp_path / "native"
    _lgtm_provisioning.render_provisioning(
        repo, native, dashboard=STUB_DASHBOARD, producer=_never_produced
    )
    dest = native / DASHBOARD_REL
    # meiva:note
    # The stale-dashboard shape: `meiva lgtm render --force` once wrote the file without recording
    # its hash, so the sidecar named older content. The next rollout's render must still land.
    dest.write_text('{"panels": ["rendered by hand on 10-02"]}\n', encoding="utf-8")

    _lgtm_provisioning.render_provisioning(
        repo, native, dashboard=STUB_DASHBOARD, producer=_never_produced
    )

    assert dest.read_text(encoding="utf-8") == STUB_DASHBOARD.content
    hashes = json.loads((native / "config/provisioning-hashes.json").read_text(encoding="utf-8"))
    assert (
        hashes["dashboards/meiva-ops-main.json"]
        == hashlib.sha256(STUB_DASHBOARD.content.encode()).hexdigest()
    )


def test_a_failed_render_keeps_the_previous_dashboard_and_reports_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = provisioning_checkout(tmp_path / "repo")
    native = tmp_path / "native"
    _lgtm_provisioning.render_provisioning(
        repo, native, dashboard=STUB_DASHBOARD, producer=_never_produced
    )
    dest = native / DASHBOARD_REL
    before = dest.read_text(encoding="utf-8")
    capsys.readouterr()

    written: list[Event] = []
    pipeline = EventPipeline(writer=written.extend)
    try:
        _lgtm_provisioning.render_provisioning(
            repo,
            native,
            dashboard=FailedDashboardRender("render exploded"),
            producer=lambda: pipeline,
        )
    finally:
        assert pipeline.stop().status is DrainStatus.COMPLETED

    assert dest.read_text(encoding="utf-8") == before
    [event] = written
    assert (event.category, event.event_name, event.level, event.source) == (
        "telemetry",
        "lgtm_dashboard_render_failed",
        "warning",
        "converge",
    )
    assert event.attributes == {"error": "render exploded", "outcome": "kept"}
    assert "keeping the installed dashboard" in capsys.readouterr().err


def test_plugins_that_failed_to_load_are_named_and_their_panels_are_missing_from_the_render(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = provisioning_checkout(tmp_path / "repo")

    _lgtm_provisioning.render_provisioning(
        repo,
        tmp_path / "native",
        dashboard=RenderedDashboard(STUB_DASHBOARD.content, ("broken-plugin", "other")),
        producer=_never_produced,
    )

    assert (
        "plugin load failures (skipped, panels missing from this render): broken-plugin, other"
        in (capsys.readouterr().err)
    )


def _never_produced() -> EventPipeline:
    raise AssertionError("a successful render reports no event")


# meiva:note
# The native backends' storage root: a configured directory is this invocation's path, its leading
# `~` expanded against the home the invocation read; an unset one is this home's own data dir.
def test_the_configured_storage_root_expands_against_the_invocations_home(tmp_path: Path) -> None:
    home, user_home = tmp_path / "home", tmp_path / "operator"

    assert (
        lgtm_native.storage_root("~/lgtm-data", home=home, user_home=user_home)
        == (user_home / "lgtm-data").resolve()
    )


def test_an_unset_storage_root_is_this_homes_own_data_directory(tmp_path: Path) -> None:
    home = tmp_path / "home"

    assert lgtm_native.storage_root("", home=home, user_home=tmp_path / "operator") == (
        home / "lgtm/native/data"
    )


def test_another_users_home_in_the_storage_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="names another user's home"):
        lgtm_native.storage_root("~someone/lgtm", home=tmp_path, user_home=tmp_path)
