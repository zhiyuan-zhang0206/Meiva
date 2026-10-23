# meiva:link cli/docs/config.meiva.okf.md
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from base.api_contracts.config import (
    ConfigAuditView,
    ConfigFieldView,
    ConfigFieldWriteResult,
    ConfigView,
    ConfigWriteResult,
)
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.host.env.tests.env_files import unit_env_aliases
from base.paths.tests.homes import unit_home as unit_home
from cli.commands.management import config as cfg
from cli.commands.management.tests.config_gateway import ConfigGateway, run_config

_CHILD = Path(__file__).with_name("child_config_cmd.py")


def _field(
    name: str,
    env_var: str,
    field_type: str,
    current_value: object,
    writable: bool,
    scope: str,
    restart_required: str,
    remote_writable: bool | None = None,
) -> ConfigFieldView:
    return ConfigFieldView(
        name=name,
        field_type=field_type,
        current_value=current_value,
        default_value=None,
        description="",
        group="",
        capability="common",
        restart_required=restart_required,
        writable=writable,
        sensitive=False,
        env_var=env_var,
        scope=scope,
        remote_writable=writable if remote_writable is None else remote_writable,
        per_agent=False,
    )


def _view(raw_overrides: dict[str, Any] | None = None) -> ConfigView:
    return ConfigView(
        fields=[
            _field("llm_model", "MEIVA_MODEL", "string", "old", True, "cluster-default", "agent"),
            _field("ops_concurrency", "MEIVA_OPS_CONCURRENCY", "int", 2, True, "host", "ops"),
            _field(
                "db_url", "MEIVA_DB_URL", "string", "postgresql://x", False, "cluster-pinned", "all"
            ),
        ],
        raw_overrides=raw_overrides or {},
        machine_capabilities=["gateway", "agent-runner"],
    )


@pytest.fixture
def captured_put(unit_home: Path) -> Generator[ConfigGateway, None, None]:
    with ConfigGateway(unit_home, _view()) as gateway:
        yield gateway


def test_set_by_env_var_name(captured_put: ConfigGateway) -> None:
    result = captured_put.run("set", "MEIVA_MODEL=new")
    assert result.returncode == 0, result.stderr
    assert captured_put.body == {"llm_model": "new"}


def test_set_by_field_name(captured_put: ConfigGateway) -> None:
    result = captured_put.run("set", "llm_model=new")
    assert result.returncode == 0, result.stderr
    assert captured_put.body == {"llm_model": "new"}


def test_set_coerces_int(captured_put: ConfigGateway) -> None:
    result = captured_put.run("set", "MEIVA_OPS_CONCURRENCY=4")
    assert result.returncode == 0, result.stderr
    assert captured_put.body == {"ops_concurrency": 4}


def test_set_list_field_writes_bare_env_and_round_trips(captured_put: ConfigGateway) -> None:
    from base.config.domains.services.settings import ServiceSettings

    captured_put.view.fields.append(
        _field(
            "im_disabled_adapters",
            "MEIVA_IM_DISABLED_ADAPTERS",
            "string",
            [],
            True,
            "cluster-pinned",
            "agent",
        )
    )
    captured_put.persist = True
    result = captured_put.run("set", "MEIVA_IM_DISABLED_ADAPTERS=weixin,feishu")

    assert result.returncode == 0, result.stderr
    assert (captured_put.home / ".env").read_text() == (
        f"MEIVA_GATEWAY_URL={captured_put.url}\nMEIVA_IM_DISABLED_ADAPTERS=weixin,feishu\n"
    )
    written = unit_env_aliases()["MEIVA_IM_DISABLED_ADAPTERS"]
    parsed = ServiceSettings.model_validate({"MEIVA_IM_DISABLED_ADAPTERS": written})
    assert parsed.im_disabled_adapters == ["weixin", "feishu"]


def test_set_sends_only_the_delta(captured_put: ConfigGateway) -> None:
    captured_put.view = _view({"llm_model": "keep"})
    result = captured_put.run("set", "MEIVA_OPS_CONCURRENCY=4")
    assert result.returncode == 0, result.stderr
    assert captured_put.body == {"ops_concurrency": 4}


def test_set_read_only_field_rejected(captured_put: ConfigGateway) -> None:
    result = captured_put.run("set", "MEIVA_DB_URL=postgresql://y")
    assert result.returncode == 1, result.stderr
    assert captured_put.body is None


def test_set_unknown_key_rejected(captured_put: ConfigGateway) -> None:
    result = captured_put.run("set", "NOT_A_KEY=x")
    assert result.returncode == 1, result.stderr
    assert captured_put.body is None


def test_set_missing_equals_rejected(captured_put: ConfigGateway) -> None:
    result = captured_put.run("set", "MEIVA_MODEL")
    assert result.returncode == 1, result.stderr
    assert captured_put.body is None


def test_set_bad_int_clean_error(captured_put: ConfigGateway) -> None:
    result = captured_put.run("set", "MEIVA_OPS_CONCURRENCY=not-a-number")
    assert result.returncode == 1, result.stderr
    assert captured_put.body is None


def test_unset_sends_explicit_null(captured_put: ConfigGateway) -> None:
    captured_put.view = _view({"llm_model": "x", "ops_concurrency": 4})
    result = captured_put.run("unset", "MEIVA_MODEL")
    assert result.returncode == 0, result.stderr
    assert captured_put.body == {"llm_model": None}


def test_set_machine_is_forwarded(captured_put: ConfigGateway) -> None:
    result = captured_put.run("set", "MEIVA_OPS_CONCURRENCY=4", machine="runner-2")
    assert result.returncode == 0, result.stderr
    assert captured_put.machine == "runner-2"


def test_set_machine_host_field_uses_remote_writable(captured_put: ConfigGateway) -> None:
    captured_put.view.fields.append(
        _field(
            "permissions_helper_enabled",
            "MEIVA_PERMISSIONS_HELPER_ENABLED",
            "bool",
            False,
            False,
            "host",
            "agent",
            remote_writable=True,
        )
    )

    result = captured_put.run(
        "set", "MEIVA_PERMISSIONS_HELPER_ENABLED=true", machine="company-mini"
    )

    assert result.returncode == 0, result.stderr
    assert captured_put.body == {"permissions_helper_enabled": True}
    assert captured_put.machine == "company-mini"


def test_set_machine_host_field_rejects_remote_read_only(captured_put: ConfigGateway) -> None:
    captured_put.view.fields.append(
        _field(
            "agent_host_health_port",
            "MEIVA_AGENT_HOST_HEALTH_PORT",
            "int",
            8114,
            True,
            "host",
            "agent",
            remote_writable=False,
        )
    )

    result = captured_put.run("set", "MEIVA_AGENT_HOST_HEALTH_PORT=18133", machine="runner-2")

    assert result.returncode == 1, result.stderr
    assert "MEIVA_AGENT_HOST_HEALTH_PORT is read-only" in result.stderr
    assert captured_put.body is None


def test_set_machine_non_host_field_still_uses_writable(captured_put: ConfigGateway) -> None:
    captured_put.view.fields.append(
        _field(
            "read_only_cluster_field",
            "MEIVA_READ_ONLY_CLUSTER_FIELD",
            "string",
            "old",
            False,
            "cluster-pinned",
            "agent",
            remote_writable=True,
        )
    )

    result = captured_put.run("set", "MEIVA_READ_ONLY_CLUSTER_FIELD=new", machine="runner-2")

    assert result.returncode == 1, result.stderr
    assert "MEIVA_READ_ONLY_CLUSTER_FIELD is read-only" in result.stderr
    assert captured_put.body is None


def test_unset_machine_host_field_rejects_remote_read_only(captured_put: ConfigGateway) -> None:
    captured_put.view.fields.append(
        _field(
            "agent_host_health_port",
            "MEIVA_AGENT_HOST_HEALTH_PORT",
            "int",
            8114,
            True,
            "host",
            "agent",
            remote_writable=False,
        )
    )

    result = captured_put.run("unset", "MEIVA_AGENT_HOST_HEALTH_PORT", machine="runner-2")

    assert result.returncode == 1, result.stderr
    assert "MEIVA_AGENT_HOST_HEALTH_PORT is read-only" in result.stderr
    assert captured_put.body is None


@pytest.mark.parametrize("scope", ["host", "cluster-pinned", "cluster-default", "agent"])
@pytest.mark.parametrize("writable", [True, False])
@pytest.mark.parametrize("remote_writable", [True, False])
@pytest.mark.parametrize("remote", [True, False])
def test_cli_field_editable_mirrors_shared_definition(
    scope: str, writable: bool, remote_writable: bool, remote: bool
) -> None:
    from base.config import ConfigFieldMeta
    from base.config.admin.editing import field_editable

    view_field = _field(
        "parity",
        "MEIVA_PARITY",
        "string",
        None,
        writable,
        scope,
        "",
        remote_writable=remote_writable,
    )
    meta = ConfigFieldMeta(
        "parity",
        "string",
        None,
        None,
        "",
        "",
        "",
        writable=writable,
        sensitive=False,
        env_var="MEIVA_PARITY",
        scope=scope,
        capability="common",
        remote_writable=remote_writable,
        per_agent=False,
    )
    assert cfg.field_editable(view_field, remote=remote) == field_editable(meta, local=not remote)


def test_unset_machine_host_field_uses_remote_writable(captured_put: ConfigGateway) -> None:
    captured_put.view.fields.append(
        _field(
            "permissions_helper_enabled",
            "MEIVA_PERMISSIONS_HELPER_ENABLED",
            "bool",
            True,
            False,
            "host",
            "agent",
            remote_writable=True,
        )
    )

    result = captured_put.run("unset", "MEIVA_PERMISSIONS_HELPER_ENABLED", machine="company-mini")

    assert result.returncode == 0, result.stderr
    assert captured_put.body == {"permissions_helper_enabled": None}
    assert captured_put.machine == "company-mini"


def _write_incident_env(home: Path) -> None:
    (home / ".env").write_text(
        "\n".join(
            (
                "MEIVA_DB_URL=postgresql://meiva@127.0.0.1:5432/meiva",
                "MEIVA_REDIS_URL=redis://127.0.0.1:6380/0",
                "MEIVA_MACHINE_SERVE_GATEWAY=true",
                "MEIVA_EXEC_TIMEOUT_SECONDS=1200",
                "MEIVA_EXEC_NODE_TIMEOUT_SECONDS=300",
                "MEIVA_OPS_CONCURRENCY=7",
            )
        )
        + "\n"
    )


def _write_valid_sandbox_env(home: Path) -> None:
    (home / ".env").write_text(
        "\n".join(
            (
                "MEIVA_MACHINE_SERVE_GATEWAY=true",
                "MEIVA_EXEC_TIMEOUT_SECONDS=300",
                "MEIVA_EXEC_NODE_TIMEOUT_SECONDS=1200",
            )
        )
        + "\n"
    )


def test_cli_module_imports_without_settings(tmp_path: Path) -> None:
    _write_incident_env(tmp_path)
    repo_root = Path(__file__).parents[4]
    result = subprocess.run(
        [sys.executable, str(_CHILD), "get"],
        cwd=repo_root,
        env={
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_HOME": str(tmp_path),
            "PATH": os.environ["PATH"],
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "MEIVA_EXEC_NODE_TIMEOUT_SECONDS" in result.stdout


@pytest.fixture
def local_env_home(unit_home: Path) -> Path:
    return unit_home


def test_gateway_base_reads_local_env_without_settings(local_env_home: Path) -> None:
    (local_env_home / ".env").write_text("MEIVA_GATEWAY_URL=http://gateway.test:8000\n")
    result = run_config(local_env_home, "gateway-base")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "http://gateway.test:8000"


def test_gateway_base_reads_the_homes_env_not_a_gateway_url_file(local_env_home: Path) -> None:
    (local_env_home / "gateway_url").write_text("http://legacy-file.test:8000\n")
    (local_env_home / ".env").write_text("MEIVA_GATEWAY_URL=http://own-cluster.test:8000\n")
    result = run_config(local_env_home, "gateway-base")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "http://own-cluster.test:8000"


def test_gateway_base_refuses_a_home_with_no_gateway_identity(local_env_home: Path) -> None:
    (local_env_home / ".env").write_text("OTHER=kept\n")
    result = run_config(local_env_home, "gateway-base")
    assert result.returncode == 1
    assert "no gateway identity" in result.stderr


def test_put_config_refuses_a_home_with_no_identity_even_with_env_override(
    captured_put: ConfigGateway,
) -> None:
    (captured_put.home / ".env").unlink()
    result = run_config(captured_put.home, "put", env={"MEIVA_GATEWAY_URL": captured_put.url})
    assert result.returncode == 1
    assert "refusing to write gateway config" in result.stderr
    assert captured_put.requests == []


def test_put_config_refuses_env_override_mismatching_home_identity(
    captured_put: ConfigGateway,
) -> None:
    (captured_put.home / ".env").write_text("MEIVA_GATEWAY_URL=http://own-cluster.test:8000\n")
    result = run_config(captured_put.home, "put", env={"MEIVA_GATEWAY_URL": captured_put.url})
    assert result.returncode == 1
    assert "does not match this home's gateway" in result.stderr
    assert captured_put.requests == []


def test_local_get_masks_sensitive(
    local_env_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    credential_path = "/private/fake-oss-credentials.json"
    (local_env_home / ".env").write_text(
        f"MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE={credential_path}\n"
    )

    rc = cfg.cmd_config_get(None, machine=None, local=True)

    assert rc == 0
    out = capsys.readouterr().out
    assert "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE" in out
    assert "••••••••" in out
    assert credential_path not in out


def test_local_set_validates_candidate_and_rejects_incident_shape(local_env_home: Path) -> None:
    _write_valid_sandbox_env(local_env_home)
    before = (local_env_home / ".env").read_bytes()
    result = run_config(local_env_home, "set", "MEIVA_EXEC_NODE_TIMEOUT_SECONDS=200")
    assert result.returncode == 1
    assert "exec_node_timeout_seconds" in result.stderr
    assert (local_env_home / ".env").read_bytes() == before


def test_local_set_applies_valid_patch(
    local_env_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (local_env_home / ".env").write_text("OTHER=kept\n")

    rc = cfg.cmd_config_set(["ops_concurrency=7"], machine=None, local=True)

    assert rc == 0
    assert unit_env_aliases()["MEIVA_OPS_CONCURRENCY"] == "7"
    assert "restart to apply" in capsys.readouterr().out


def test_the_offsite_keys_are_official_local_config(local_env_home: Path) -> None:
    (local_env_home / ".env").write_text("MEIVA_MACHINE_SERVE_GATEWAY=true\nOTHER=kept\n")
    credentials = local_env_home / "oss-credentials.json"
    keys = {
        "MEIVA_BACKUP_OFFSITE_ENDPOINT": "https://oss-cn-shanghai.aliyuncs.com",
        "MEIVA_BACKUP_OFFSITE_BUCKET": "backups",
        "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE": str(credentials),
    }

    rc = cfg.cmd_config_set([f"{key}={value}" for key, value in keys.items()], None, local=True)

    assert rc == 0
    aliases = unit_env_aliases()
    assert {key: aliases[key] for key in keys} == keys
    assert "OTHER" in aliases

    assert cfg.cmd_config_unset(list(keys), None, local=True) == 0
    assert not set(keys) & set(unit_env_aliases())


def test_the_walg_key_is_official_local_config(local_env_home: Path) -> None:
    (local_env_home / ".env").write_text("MEIVA_MACHINE_SERVE_GATEWAY=true\nOTHER=kept\n")
    config_file = local_env_home / "walg.json"

    assert cfg.cmd_config_set([f"MEIVA_WALG_CONFIG_FILE={config_file}"], None, local=True) == 0
    aliases = unit_env_aliases()
    assert aliases["MEIVA_WALG_CONFIG_FILE"] == str(config_file)
    assert "OTHER" in aliases

    assert cfg.cmd_config_unset(["MEIVA_WALG_CONFIG_FILE"], None, local=True) == 0
    assert "MEIVA_WALG_CONFIG_FILE" not in unit_env_aliases()


def test_local_set_can_pin_the_hosted_runner_health_port(
    local_env_home: Path,
) -> None:
    (local_env_home / ".env").write_text("OTHER=kept\n")

    rc = cfg.cmd_config_set(["MEIVA_AGENT_HOST_HEALTH_PORT=18133"], machine=None, local=True)

    assert rc == 0
    aliases = unit_env_aliases()
    assert aliases["MEIVA_AGENT_HOST_HEALTH_PORT"] == "18133"
    assert "OTHER" in aliases


def test_local_unset_removes_key(local_env_home: Path) -> None:
    (local_env_home / ".env").write_text("MEIVA_OPS_CONCURRENCY=7\n")

    rc = cfg.cmd_config_unset(["MEIVA_OPS_CONCURRENCY"], machine=None, local=True)

    assert rc == 0
    assert "MEIVA_OPS_CONCURRENCY" not in unit_env_aliases()


def test_local_set_repairs_incident_env(local_env_home: Path) -> None:
    _write_incident_env(local_env_home)
    before = (local_env_home / ".env").read_bytes()
    repo_root = Path(__file__).parents[4]
    result = subprocess.run(
        [sys.executable, str(_CHILD), "set", "MEIVA_EXEC_NODE_TIMEOUT_SECONDS=2000"],
        cwd=repo_root,
        env={
            "MEIVA_CONFIG_FETCH": "skip",
            "MEIVA_HOME": str(local_env_home),
            "PATH": os.environ["PATH"],
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr

    assert unit_env_aliases()["MEIVA_EXEC_NODE_TIMEOUT_SECONDS"] == "2000.0"

    assert unit_env_aliases()["MEIVA_OPS_CONCURRENCY"] == "7"
    assert (local_env_home / ".env").read_bytes() != before


def test_set_rejected_result_returns_1(captured_put: ConfigGateway) -> None:
    captured_put.result = ConfigWriteResult(
        applied=False,
        results={"ops_concurrency": ConfigFieldWriteResult(ok=False, reason="out of range")},
        restart_required=[],
    )
    result = captured_put.run("set", "MEIVA_OPS_CONCURRENCY=4")
    assert result.returncode == 1
    assert "out of range" in result.stderr


def test_get_single_key(captured_put: ConfigGateway) -> None:
    result = captured_put.run("get", "MEIVA_MODEL")
    assert result.returncode == 0, result.stderr
    assert "MEIVA_MODEL" in result.stdout
    assert "old" in result.stdout


def test_config_audit_local_reads_newest_first_and_filters(
    local_env_home: Path, capsys: pytest.CaptureFixture[str], machine_identity: MachineIdentity
) -> None:
    from ops.env_audit import record_env_write

    env_path = local_env_home / ".env"
    env_path.write_text("MEIVA_MODEL=m\n")
    record_env_write(
        env_path,
        {"MEIVA_MODEL"},
        set(),
        site="first",
        changes=[{"alias": "MEIVA_MODEL", "old": "a", "new": "b"}],
    )
    record_env_write(env_path, {"MEIVA_FEISHU_APP_ID"}, set(), site="second")
    machine_identity.set(name="audit-fixture")

    assert cfg.cmd_config_audit(last=10, key=None, machine=None, identity=machine_identity) == 0
    out = capsys.readouterr().out
    assert "site=first" in out and "site=second" in out
    assert out.index("site=second") < out.index("site=first")
    assert "MEIVA_MODEL: a -> b" in out

    assert (
        cfg.cmd_config_audit(
            last=10, key="MEIVA_FEISHU_APP_ID", machine=None, identity=machine_identity
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "site=second" in out
    assert "site=first" not in out


def test_config_audit_machine_fetches_via_gateway(captured_put: ConfigGateway) -> None:
    captured_put.audit = ConfigAuditView(
        records=[
            {
                "ts": "2026-09-16T00:00:00+00:00",
                "machine": "m1",
                "site": "remote-site",
                "actor": "user_session:administrator",
                "keys_written": ["MEIVA_MODEL", "ANTHROPIC_API_KEY"],
                "keys_removed": [],
                "changed": [
                    {
                        "alias": "MEIVA_MODEL",
                        "scope": "host",
                        "sensitive": False,
                        "old": "a",
                        "new": "b",
                    },
                    {
                        "alias": "ANTHROPIC_API_KEY",
                        "scope": "cluster",
                        "sensitive": True,
                        "old": None,
                        "new": None,
                    },
                ],
            }
        ]
    )

    result = captured_put.run("audit", "5", machine="m1")
    assert result.returncode == 0, result.stderr
    assert captured_put.requests == [("GET", "/api/config/audit?last=5&machine=m1")]
    out = result.stdout
    assert "[m1]" in out and "remote-site" in out
    assert "actor=user_session:administrator" in out
    assert "MEIVA_MODEL: a -> b" in out
    assert "ANTHROPIC_API_KEY: (withheld)" in out


def test_config_audit_rejects_out_of_range_last(
    capsys: pytest.CaptureFixture[str], machine_identity: MachineIdentity
) -> None:
    assert cfg.cmd_config_audit(last=0, key=None, machine=None, identity=machine_identity) == 1
    assert "--last must be between 1 and 200" in capsys.readouterr().err

    assert cfg.cmd_config_audit(last=201, key=None, machine="m1", identity=machine_identity) == 1
    assert "--last must be between 1 and 200" in capsys.readouterr().err
