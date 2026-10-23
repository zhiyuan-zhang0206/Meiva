from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from base.cluster import derive
from base.config import Settings
from base.config.service_read import ConfigAuthority
from base.log.tests.log_records import LogRecords
from base.log.tests.log_records import loguru_records as loguru_records

_OWNER_URL = "postgresql://meiva:owner-password@127.0.0.1:5433/meiva"
_RUNNER_URL = "postgresql://meiva_runner:runner-password@127.0.0.1:5433/meiva"


_GATEWAY = {"MEIVA_MACHINE_SERVE_GATEWAY": "true"}


def test_projects_owner_url_to_runner_identity() -> None:
    env_file = {"MEIVA_DB_URL": _OWNER_URL, "MEIVA_RUNNER_DB_PASSWORD": "runner-password"}

    assert derive.runner_db_url_projection(_GATEWAY, env_file=env_file) == _RUNNER_URL


def test_a_gateway_flag_in_the_env_file_makes_the_unit_a_gateway_home() -> None:
    env_file = {
        "MEIVA_MACHINE_SERVE_GATEWAY": "true",
        "MEIVA_DB_URL": _OWNER_URL,
        "MEIVA_RUNNER_DB_PASSWORD": "runner-password",
    }

    assert derive.runner_db_url_projection({}, env_file=env_file) == _RUNNER_URL


def test_missing_runner_password_fails_loudly() -> None:
    with pytest.raises(RuntimeError, match="MEIVA_RUNNER_DB_PASSWORD is missing"):
        derive.runner_db_url_projection(_GATEWAY, env_file={"MEIVA_DB_URL": _OWNER_URL})


def test_gateway_url_and_password_come_from_one_snapshot() -> None:
    env_file = {
        "MEIVA_DB_URL": "postgresql://meiva:new-owner@db-new:6000/new-database",
        "MEIVA_RUNNER_DB_PASSWORD": "new-runner",
    }
    assert derive.runner_db_url_projection(_GATEWAY, env_file=env_file) == (
        "postgresql://meiva_runner:new-runner@db-new:6000/new-database"
    )


def test_missing_snapshot_url_refuses() -> None:
    with pytest.raises(RuntimeError, match="MEIVA_DB_URL is missing"):
        derive.runner_db_url_projection(
            _GATEWAY, env_file={"MEIVA_RUNNER_DB_PASSWORD": "new-runner"}
        )


def test_pure_runner_never_projects_a_login() -> None:
    env_file = {"MEIVA_DB_URL": _OWNER_URL, "MEIVA_RUNNER_DB_PASSWORD": "runner-password"}
    with pytest.raises(RuntimeError, match="installed unit capability"):
        derive.runner_db_url_projection({}, env_file=env_file)


def test_malformed_config_read_does_not_log_raw_credentials(
    tmp_path: Path, loguru_records: LogRecords
) -> None:
    runtime = Settings(profile=None)
    authority = ConfigAuthority(runtime, runtime, tmp_path / ".env")
    authority.env_path.write_text("MEIVA_DB_URL=postgresql://owner-password@[::1\n")
    loguru_records.clear()

    with pytest.raises(ValidationError):
        authority.current_field_values()
    assert loguru_records.messages("WARNING") == []
    assert loguru_records.messages("DEBUG") == []
