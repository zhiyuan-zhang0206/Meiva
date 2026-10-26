# meiva:note
# Root diagnostics use storage protocols without repairing permissions.


from __future__ import annotations

from functools import partial

from base import cluster
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.deploy.tests.containers import free_port, redis_server
from cli.commands.data_plane._pooler_stop import OwnedPooler
from cli.commands.data_plane.tests.native_cluster import redis_command, redis_from_url, redis_ping
from cli.commands.data_plane.tests.native_processes import native_pooler as native_pooler
from services.supervision.meiva_root.health import ProbeRunner
from services.supervision.meiva_root_glue import diagnostic_probes as probes


async def test_native_redis_diagnostic_uses_authenticated_ping_without_acl_or_config_writes(
    redis_access: None,
) -> None:
    with redis_server() as url, redis_from_url(url, decode_responses=True) as client:
        redis_command(
            client, "ACL", "SETUSER", "meiva_probe_fixture", "on", ">fixture-password", "+ping"
        )
        runtime_url = url.replace("redis://", "redis://meiva_probe_fixture:fixture-password@")
        configuration = redis_command(client, "CONFIG", "GET", "*")
        acl = redis_command(client, "ACL", "LIST")
        result = await ProbeRunner().observe(partial(probes.redis_acl, redis_url=runtime_url), 5)
        assert result.verdict.value == "alive", result.detail
        assert redis_ping(client), "diagnostics must leave the native server running"
        assert redis_command(client, "ACL", "LIST") == acl
        assert redis_command(client, "CONFIG", "GET", "*") == configuration


async def test_absent_native_redis_diagnostic_is_down(redis_access: None) -> None:
    url = f"redis://127.0.0.1:{free_port()}/0"
    result = await ProbeRunner().observe(partial(probes.redis_acl, redis_url=url), 5)
    assert result.verdict.value == "down", result.detail
    assert "PING failed" in result.detail


async def test_native_pooler_diagnostic_authenticates_without_config_writes(
    native_pooler: tuple[OwnedPooler, str, str],
    machine_identity: MachineIdentityReader,
) -> None:
    custodian, _pooled, _direct = native_pooler
    home = custodian.config.parent.parent
    ports = cluster.new_home_ports()
    ports["pgbouncer"] = custodian.port
    record = cluster.ClusterRecord(ports=ports, gateway_home=str(home), created_at="fixture")
    before = custodian.config.read_bytes()
    # meiva:note
    # The fixture pooler trusts its userlist; the probe authenticates as the
    # admin-console operator entry with the home's recorded credential.
    result = await ProbeRunner().observe(
        partial(
            probes.pgbouncer,
            machine=machine_identity,
            home=home,
            record=record,
            admin_password="fixture-admin",  # noqa: S106
        ),
        5,
    )
    assert result.verdict.value == "alive", result.detail
    assert custodian.identity.live()
    assert custodian.config.read_bytes() == before
