"""HTTP regression coverage for candidate validation before cluster config writes."""

from __future__ import annotations

from pathlib import Path

import pytest

from base.config import runtime_config
from base.host.env.tests.env_files import unit_env_aliases
from gateway.tests.auth.gateway_config import gateway_test_client


@pytest.fixture
def configured_home(unit_home: Path) -> Path:
    """A gateway home whose `.env` carries a valid sandbox timeout pair."""
    runtime_config.write_fields(
        {"exec_timeout_seconds": 300, "exec_node_timeout_seconds": 1200}, set()
    )
    return unit_home


def test_put_rejects_invalid_candidate_without_writing(configured_home: Path) -> None:
    """A patch that breaks a cross-field invariant never reaches the `.env`."""
    env_path = configured_home / ".env"
    before = env_path.read_bytes()

    with gateway_test_client() as client:
        response = client.put("/api/config", json={"exec_node_timeout_seconds": 200})

    assert response.status_code == 400, response.text
    assert "candidate config rejected" in response.json()["detail"]
    assert "exec_node_timeout_seconds" in response.json()["detail"]
    assert env_path.read_bytes() == before


# meiva:note
# A cluster-pinned edit lands in one atomic PUT. Keys a retired feature left in the `.env` are
# inert: they neither block the write nor change how it validates.
def test_put_sets_a_cluster_pinned_key_beside_retired_keys(configured_home: Path) -> None:
    env_path = configured_home / ".env"
    env_path.write_text(
        env_path.read_text()
        + "MEIVA_PITR_ENABLED=true\n"
        + "MEIVA_PITR_STORE_BACKEND=oss\n"
        + "MEIVA_PITR_GCS_BUCKET=retired\n"
        + "MEIVA_PITR_SPOOL_HARD_BYTES=2362232013\n"
        + "MEIVA_WALG_CONFIG_FILE=/retired/walg.json\n"
    )
    with gateway_test_client() as client:
        response = client.put("/api/config", json={"backup_keep": 5})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] is True
    assert "gateway" in body["restart_required"]
    aliases = unit_env_aliases()
    assert aliases["MEIVA_BACKUP_KEEP"] == "5"
    assert aliases["MEIVA_PITR_STORE_BACKEND"] == "oss"
    assert aliases["MEIVA_WALG_CONFIG_FILE"] == "/retired/walg.json"


# meiva:note
# A key is removed through the same official path.
def test_put_unsets_a_cluster_pinned_key(configured_home: Path) -> None:
    runtime_config.write_fields({"backup_keep": 3}, set())

    with gateway_test_client() as client:
        response = client.put("/api/config", json={"backup_keep": None})

    assert response.status_code == 200, response.text
    assert "MEIVA_BACKUP_KEEP" not in unit_env_aliases()


# meiva:note
# A host-only edit lands without a cluster candidate (see `test_write_plan`).
def test_host_only_put_skips_cluster_candidate_validation(
    configured_home: Path,
) -> None:
    with gateway_test_client() as client:
        response = client.put("/api/config", json={"ops_concurrency": 4})

    assert response.status_code == 200, response.text
    assert response.json()["applied"] is True
    assert unit_env_aliases()["MEIVA_OPS_CONCURRENCY"] == "4"
