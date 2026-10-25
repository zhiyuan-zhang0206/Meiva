# meiva:note
# Root diagnostics use storage protocols without repairing permissions.


from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from base import cluster
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.deploy.tests.containers import free_port, redis_server
from base.host.db_authority import delivery
from cli.commands.data_plane._pooler_stop import OwnedPooler
from cli.commands.data_plane.tests.native_cluster import redis_command, redis_from_url, redis_ping
from cli.commands.data_plane.tests.native_processes import native_pooler as native_pooler
from services.supervision.meiva_root.health import ProbeRunner
from services.supervision.meiva_root_glue import diagnostic_probes as probes

_CHILD = Path(__file__).with_name("redis_diagnostic_child.py")
_REPO = Path(__file__).resolve().parents[4]


# meiva:note
# The Redis diagnostic as a child process observed it with `redis_url` as the unit's endpoint.
# The endpoint is a cluster-scope setting, so it reaches the process through the unit's `.env`.
def _redis_diagnostic_in_child(home: Path, redis_url: str) -> dict[str, str]:
    (home / ".env").write_text(f"MEIVA_REDIS_URL={redis_url}\nMEIVA_REDIS_ADMIN_PASSWORD=\n")
    base_env = {key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR") if key in os.environ}
    done = subprocess.run(
        [sys.executable, str(_CHILD)],
        cwd=_REPO,
        env={**base_env, "MEIVA_HOME": str(home), "MEIVA_REDIS_URL": redis_url},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_native_redis_diagnostic_uses_authenticated_ping_without_acl_or_config_writes(
    tmp_path: Path,
    redis_access: None,
) -> None:
    with redis_server() as url, redis_from_url(url, decode_responses=True) as client:
        redis_command(
            client, "ACL", "SETUSER", "meiva_probe_fixture", "on", ">fixture-password", "+ping"
        )
        runtime_url = url.replace("redis://", "redis://meiva_probe_fixture:fixture-password@")
        configuration = redis_command(client, "CONFIG", "GET", "*")
        acl = redis_command(client, "ACL", "LIST")
        result = _redis_diagnostic_in_child(tmp_path, runtime_url)
        assert result["verdict"] == "alive", result["detail"]
        assert redis_ping(client), "diagnostics must leave the native server running"
        assert redis_command(client, "ACL", "LIST") == acl
        assert redis_command(client, "CONFIG", "GET", "*") == configuration


def test_absent_native_redis_diagnostic_is_down(tmp_path: Path, redis_access: None) -> None:
    port = free_port()
    result = _redis_diagnostic_in_child(tmp_path, f"redis://127.0.0.1:{port}/0")
    assert result["verdict"] == "down", result["detail"]
    assert "PING failed" in result["detail"]


async def test_native_pooler_diagnostic_authenticates_without_config_writes(
    native_pooler: tuple[OwnedPooler, str, str],
    monkeypatch: pytest.MonkeyPatch,
    machine_identity: MachineIdentityReader,
) -> None:
    custodian, _pooled, _direct = native_pooler
    ports = cluster.new_home_ports()
    ports["pgbouncer"] = custodian.port
    record = cluster.ClusterRecord(
        ports=ports, gateway_home=str(custodian.config.parent.parent), created_at="fixture"
    )
    monkeypatch.setattr(cluster, "get_record", Mock(return_value=record))
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")
    # meiva:note
    # The fixture pooler trusts its userlist; the probe authenticates as the
    # admin-console operator entry with the home's recorded credential.
    monkeypatch.setattr(
        delivery,
        "read_pooler_admin",
        Mock(return_value=SimpleNamespace(password="fixture-admin")),  # noqa: S106
    )
    before = custodian.config.read_bytes()
    result = await ProbeRunner().observe(partial(probes.pgbouncer, machine=machine_identity), 5)
    assert result.verdict.value == "alive", result.detail
    assert custodian.identity.live()
    assert custodian.config.read_bytes() == before
