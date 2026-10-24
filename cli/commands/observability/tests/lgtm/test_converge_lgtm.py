# meiva:note
# The native LGTM renderer over the checkout's real templates and provisioning tree.
#
# Every case renders into real files under `tmp_path` from values the test passes: the settings,
# the direct database URL, the dashboard outcome and the machine. What the URLs, the substitutions
# and the warnings are is decided under `plans/` and tested there; these cases prove the shell reads
# the templates, writes the tree, restores drifted files and prints what the plans say.

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from cli.commands.observability import lgtm_native
from cli.commands.observability.inputs import LgtmSettings
from cli.commands.observability.plans.observability_config import AlertWebhookInputs
from cli.commands.observability.tests.lgtm.lgtm_harness import (
    LOOPBACK_DB,
    STUB_DASHBOARD,
    LgtmUnit,
    lgtm_unit,
)
from cli.commands.observability.tests.lgtm.lgtm_harness import (
    operator_pipeline as operator_pipeline,
)
from cli.commands.observability.tests.observability_inputs import lgtm_settings, observability
from cli.database.tests.command_inputs import command_runtime as command_runtime

__all__ = ["lgtm_unit"]

# meiva:note
# S104-flagged literal reused by the cases below: a config value under test, not a bind.
WILDCARD = "0.0.0.0"  # noqa: S104


def settings_with(**changes: object) -> LgtmSettings:
    return lgtm_settings(observability=observability(**changes))


def datasources(unit: LgtmUnit) -> dict[str, str]:
    rendered = yaml.safe_load(
        (unit.native / "config/provisioning/datasources/datasources.yml").read_text(
            encoding="utf-8"
        )
    )
    return {entry["uid"]: entry["url"] for entry in rendered["datasources"]}


def test_grafana_binds_the_configured_listen_host_and_port(lgtm_unit: LgtmUnit) -> None:
    lgtm_unit.render(settings=settings_with(lgtm_grafana_listen_host=WILDCARD))
    grafana_ini = (lgtm_unit.native / "config/grafana.ini").read_text(encoding="utf-8")

    assert f"http_addr = {WILDCARD}" in grafana_ini
    assert "http_port = 3003" in grafana_ini
    # meiva:note
    # The native deployment provisions no preinstalled plugins, so the Grafana 13 background
    # installer must be off: its in-flight work can hold a normal SIGTERM past the unit's
    # shutdown deadline.
    assert "preinstall_disabled = true" in grafana_ini

    lgtm_unit.render(settings=settings_with(lgtm_grafana_listen_host="10.0.0.5"))

    assert "http_addr = 10.0.0.5" in (lgtm_unit.native / "config/grafana.ini").read_text(
        encoding="utf-8"
    )


def test_the_loki_and_grafana_ports_follow_the_settings_not_the_read_urls(
    lgtm_unit: LgtmUnit,
) -> None:
    lgtm_unit.render(
        settings=settings_with(
            lgtm_listen_host="127.0.0.1",
            lgtm_grafana_listen_host="127.0.0.1",
            lgtm_loki_port=53100,
            lgtm_loki_grpc_port=59095,
            lgtm_grafana_port=53003,
            telemetry_loki_url="https://query.example/loki",
        )
    )

    loki = yaml.safe_load((lgtm_unit.native / "config/loki.yaml").read_text(encoding="utf-8"))
    assert loki["server"]["http_listen_port"] == 53100
    assert loki["server"]["grpc_listen_port"] == 59095
    assert "http_port = 53003" in (lgtm_unit.native / "config/grafana.ini").read_text(
        encoding="utf-8"
    )
    run_script = (lgtm_unit.native / "grafana/run.sh").read_text(encoding="utf-8")
    assert "GRAFANA_ROOT_URL:-http://localhost:53003}" in run_script


def test_a_remote_observatory_is_rendered_into_the_datasources_and_the_webhook(
    lgtm_unit: LgtmUnit,
) -> None:
    lgtm_unit.machine.set(host="10.0.0.10")

    lgtm_unit.render(
        settings=settings_with(observability_url="http://10.0.0.46"),
        direct_db_url="postgresql://grafana_ro@10.0.0.72:5433/meiva_main",
    )

    env = lgtm_unit.runtime_env()
    assert env["MEIVA_TELEMETRY_LOKI_URL"] == "http://10.0.0.46:3100"
    assert env["MEIVA_TELEMETRY_PROMETHEUS_URL"] == "http://10.0.0.46:9090"
    # meiva:note
    # PG is the cluster's own database: it follows the data-plane URL, not the observatory, so
    # moving the observatory leaves the SQL datasource on the gateway.
    assert env["MEIVA_PG_URL"] == "10.0.0.72:5433"
    assert env["MEIVA_ALERTS_WEBHOOK_URL"] == "http://10.0.0.10:8000/api/alerts"
    by_uid = datasources(lgtm_unit)
    assert by_uid["loki"] == "$__env{MEIVA_TELEMETRY_LOKI_URL}"
    assert by_uid["prometheus"] == "$__env{MEIVA_TELEMETRY_PROMETHEUS_URL}"
    assert by_uid["ops"] == "$__env{MEIVA_PG_URL}"


def test_without_an_observatory_the_webhook_is_loopback_even_for_a_routable_machine(
    lgtm_unit: LgtmUnit,
) -> None:
    lgtm_unit.machine.set(host="10.0.0.10")

    lgtm_unit.render()

    env = lgtm_unit.runtime_env()
    assert env["MEIVA_ALERTS_WEBHOOK_URL"] == "http://127.0.0.1:8000/api/alerts"
    assert "10.0.0.10" not in (lgtm_unit.native / "config/runtime.env").read_text(encoding="utf-8")
    contact = (lgtm_unit.native / "config/provisioning/alerting/contact.yml").read_text(
        encoding="utf-8"
    )
    assert "$__env{MEIVA_ALERTS_WEBHOOK_URL}" in contact
    assert "{{" not in contact


@pytest.mark.parametrize("path", ["$MEIVA_REVIEW_LITERAL", "$(touch${IFS}webhook-command-ran)"])
def test_the_real_grafana_launcher_receives_a_webhook_url_with_shell_syntax_as_literal_text(
    lgtm_unit: LgtmUnit, tmp_path: Path, path: str
) -> None:
    base = f"https://gateway.test:20016/tenant/{path}"
    lgtm_unit.render(
        settings=replace(
            settings_with(observability_url="http://observatory.test"),
            webhook=AlertWebhookInputs(gateway_url=base, gateway_port=8000),
        )
    )
    executable = lgtm_unit.native / "grafana-home/bin/grafana"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\nimport json, os\n"
        "print(json.dumps(os.environ['MEIVA_ALERTS_WEBHOOK_URL']))\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    result = subprocess.run(
        ["bash", str(lgtm_unit.native / "grafana/run.sh")],
        cwd=tmp_path,
        env={**os.environ, "MEIVA_REVIEW_LITERAL": "expanded"},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "webhook-command-ran").exists()
    assert json.loads(result.stdout) == f"{base}/api/alerts"


@pytest.mark.parametrize(
    ("observatory", "db_url", "expected"),
    [
        ("", "postgresql://reader:synthetic-secret@127.0.0.1:5433/meiva", "127.0.0.1:5433"),
        ("", "postgresql://reader:synthetic-secret@db.test:25432/meiva", "db.test:25432"),
        ("", "postgresql://reader:synthetic-secret@[::1]:20027/meiva", "[::1]:20027"),
        ("", "postgresql:///meiva?host=/tmp/meiva-test&port=20027", "127.0.0.1:20027"),
        ("", "postgresql://reader@db.test/meiva?port=25432", "db.test:25432"),
        ("", "postgresql://reader@db.test/meiva", "db.test:5432"),
        ("http://observatory.test", "postgresql://reader@db.test:25432/meiva", "db.test:25432"),
    ],
)
def test_the_pg_endpoint_follows_the_connection_url_and_never_carries_its_credentials(
    lgtm_unit: LgtmUnit,
    capsys: pytest.CaptureFixture[str],
    observatory: str,
    db_url: str,
    expected: str,
) -> None:
    lgtm_unit.render(settings=settings_with(observability_url=observatory), direct_db_url=db_url)

    assert lgtm_unit.runtime_env()["MEIVA_PG_URL"] == expected
    assert "synthetic-secret" not in (lgtm_unit.native / "config/runtime.env").read_text(
        encoding="utf-8"
    )
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err
    assert lgtm_unit.runtime_env()["MEIVA_TELEMETRY_LOKI_URL"] == (
        "http://observatory.test:3100" if observatory else "http://127.0.0.1:3100"
    )


def test_a_loopback_database_under_a_remote_observatory_renders_loopback_and_warns(
    lgtm_unit: LgtmUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    lgtm_unit.render(
        settings=settings_with(observability_url="http://10.0.0.46"),
        direct_db_url="postgresql:///meiva_main?host=/tmp/meiva-pg-meiva-test&port=5433",
    )

    assert lgtm_unit.runtime_env()["MEIVA_PG_URL"] == "127.0.0.1:5433"
    assert lgtm_unit.runtime_env()["MEIVA_TELEMETRY_LOKI_URL"] == "http://10.0.0.46:3100"
    assert "data-plane db_url" in capsys.readouterr().err


def test_a_malformed_observability_url_warns_and_falls_back_to_loopback(
    lgtm_unit: LgtmUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    lgtm_unit.render(settings=settings_with(observability_url="10.0.0.1:1234"))

    env = lgtm_unit.runtime_env()
    assert env["MEIVA_TELEMETRY_LOKI_URL"] == "http://127.0.0.1:3100"
    assert env["MEIVA_TELEMETRY_PROMETHEUS_URL"] == "http://127.0.0.1:9090"
    assert "malformed" in capsys.readouterr().err


def test_a_drifted_provisioning_file_is_restored_from_the_checkout_and_reported(
    lgtm_unit: LgtmUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    lgtm_unit.render()
    path = lgtm_unit.native / "config/provisioning/datasources/datasources.yml"
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace("$__env{MEIVA_TELEMETRY_LOKI_URL}", "http://user.example:3100"),
        encoding="utf-8",
    )
    capsys.readouterr()

    lgtm_unit.render()

    assert path.read_text(encoding="utf-8") == original
    assert "modified locally; overwritten from the rendered source" in capsys.readouterr().err


def test_a_rendered_file_whose_source_vanished_is_removed_when_untouched(
    lgtm_unit: LgtmUnit,
) -> None:
    lgtm_unit.render()
    stale = lgtm_unit.native / "config/provisioning/datasources/old.yml"
    stale.write_text("stale", encoding="utf-8")
    hashes_path = lgtm_unit.native / "config/provisioning-hashes.json"
    hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    hashes["datasources/old.yml"] = hashlib.sha256(b"stale").hexdigest()
    hashes_path.write_text(json.dumps(hashes), encoding="utf-8")

    lgtm_unit.render()

    assert not stale.exists()


def test_an_orphan_the_user_edited_is_kept_and_reported(
    lgtm_unit: LgtmUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    lgtm_unit.render()
    orphan = lgtm_unit.native / "config/provisioning/datasources/old.yml"
    orphan.write_text("edited", encoding="utf-8")
    hashes_path = lgtm_unit.native / "config/provisioning-hashes.json"
    hashes = json.loads(hashes_path.read_text(encoding="utf-8"))
    hashes["datasources/old.yml"] = hashlib.sha256(b"what converge wrote").hexdigest()
    hashes_path.write_text(json.dumps(hashes), encoding="utf-8")
    capsys.readouterr()

    lgtm_unit.render()

    assert orphan.read_text(encoding="utf-8") == "edited"
    assert "has no source template anymore but was modified locally; kept" in (
        capsys.readouterr().err
    )


def test_a_rendered_loki_config_that_breaks_the_deploy_invariants_writes_nothing(
    lgtm_unit: LgtmUnit,
) -> None:
    # meiva:note
    # A checkout whose Loki template pins the wrong retention: the deploy invariants refuse the
    # rendered config before any file is written, so a half-rendered tree never reaches the
    # services.
    repo = lgtm_unit.home / "repo"
    source = repo / "deploy/lgtm/native/config"
    source.mkdir(parents=True)
    for name in ("prometheus.yml", "grafana.ini", "runtime.env", "run.sh"):
        (source / name).write_text("x\n", encoding="utf-8")
    (source / "loki.yaml").write_text(
        "limits_config:\n  retention_period: 1h\n  max_query_series: 1\nquerier:\n  max_concurrent: 1\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="retention_period"):
        lgtm_native.render_configs(
            repo,
            lgtm_unit.native,
            lgtm_unit.home,
            settings=lgtm_settings(),
            storage_dir=lgtm_unit.native / "data",
            direct_db_url=LOOPBACK_DB,
            dashboard=STUB_DASHBOARD,
            producer=lgtm_unit.producer,
            machine=lgtm_unit.machine,
        )

    assert not (lgtm_unit.native / "config").exists()


def test_the_grafana_admin_password_is_written_owner_only_when_configured(
    lgtm_unit: LgtmUnit,
) -> None:
    lgtm_native.render_grafana_admin_password(lgtm_unit.native, "fake-key-for-test")

    credential = lgtm_unit.native / "grafana/admin_password"
    assert credential.read_text(encoding="utf-8") == "fake-key-for-test\n"
    assert credential.stat().st_mode & 0o777 == 0o600


def test_an_unconfigured_grafana_password_leaves_no_credential_file(lgtm_unit: LgtmUnit) -> None:
    lgtm_unit.render()
    lgtm_native.render_grafana_admin_password(lgtm_unit.native, None)

    credential = lgtm_unit.native / "grafana/admin_password"
    run_script = (lgtm_unit.native / "grafana/run.sh").read_text(encoding="utf-8")
    assert not credential.exists()
    assert f'if [[ -f "{credential}" ]]; then' in run_script
    assert f'cat "{credential}"' not in run_script


# meiva:note
# The bot token is a fixture value, never a credential.
def test_the_telegram_contact_env_exists_only_while_the_home_has_a_bot(
    lgtm_unit: LgtmUnit,
) -> None:
    lgtm_unit.render()
    env_file = lgtm_unit.native / "grafana/telegram.env"

    lgtm_native.render_grafana_telegram_env(
        lgtm_unit.native,
        token="fake-bot-token-for-test",  # noqa: S106
        owner=42,
    )

    assert env_file.read_text(encoding="utf-8") == (
        "export MEIVA_ALERTS_TELEGRAM_BOT_TOKEN=fake-bot-token-for-test\n"
        "export MEIVA_ALERTS_TELEGRAM_CHAT_ID=42\n"
    )
    assert env_file.stat().st_mode & 0o777 == 0o600
    run_script = (lgtm_unit.native / "grafana/run.sh").read_text(encoding="utf-8")
    assert f'. "{env_file}"' in run_script

    lgtm_native.render_grafana_telegram_env(lgtm_unit.native, token="", owner=0)

    assert not env_file.exists()
    assert 'MEIVA_ALERTS_TELEGRAM_BOT_TOKEN:-unconfigured}"' in run_script


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        (
            settings_with(
                telemetry_tempo_query_url="http://127.0.0.1:3200",
                telemetry_tempo_endpoint="http://tempo.example:14318",
            ),
            ["MEIVA_TELEMETRY_TEMPO_QUERY_URL resolves to http://127.0.0.1:3200"],
        ),
        (
            settings_with(lgtm_listen_host="10.0.0.5", lgtm_grafana_listen_host=WILDCARD),
            ["MEIVA_TELEMETRY_LOKI_URL", "MEIVA_TELEMETRY_PROMETHEUS_URL"],
        ),
        (settings_with(), []),
        (
            settings_with(
                lgtm_listen_host="10.0.0.5",
                telemetry_loki_url="http://10.0.0.5:3100",
                telemetry_prometheus_url="http://10.0.0.5:9090",
            ),
            [],
        ),
    ],
    ids=["tempo-topology", "widened-listen", "defaults", "read-urls-follow-the-listen-host"],
)
def test_a_render_prints_the_warnings_its_plans_name_and_nothing_else(
    lgtm_unit: LgtmUnit,
    capsys: pytest.CaptureFixture[str],
    settings: LgtmSettings,
    expected: list[str],
) -> None:
    lgtm_unit.render(settings=settings)

    stderr = capsys.readouterr().err
    assert len(stderr.strip().splitlines()) == len(expected)
    for fragment in expected:
        assert fragment in stderr


def test_a_value_that_disagrees_with_the_env_file_is_warned_about(
    lgtm_unit: LgtmUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    lgtm_unit.home.mkdir(parents=True)
    (lgtm_unit.home / ".env").write_text(
        "MEIVA_TELEMETRY_TEMPO_QUERY_URL=http://127.0.0.1:3200\n", encoding="utf-8"
    )

    lgtm_unit.render(settings=settings_with(telemetry_tempo_query_url="http://10.55.0.9:3200"))

    err = capsys.readouterr().err
    assert "MEIVA_TELEMETRY_TEMPO_QUERY_URL resolved to http://10.55.0.9:3200" in err
    assert "inherited environment value is in effect" in err


def test_a_home_without_an_env_file_has_nothing_to_diverge_from(
    lgtm_unit: LgtmUnit, capsys: pytest.CaptureFixture[str]
) -> None:
    lgtm_unit.render()

    assert capsys.readouterr().err == ""
