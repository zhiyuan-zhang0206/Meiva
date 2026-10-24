from __future__ import annotations

import json
from pathlib import Path

from base.cluster.dataplane.redis_bridge import RedisBridgeEndpoint, redis_bridge_endpoint
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from services.supervision.roster.composition import build_services, redis_bridge_launch
from services.supervision.roster.gate_plan import BridgeFact, gate_reason
from services.supervision.roster.service_spec import ServiceSpec


def _bridge() -> ServiceSpec:
    return next(service for service in build_services() if service.session == "redis-bridge")


def _remote_data_plane() -> bool:
    return False


def test_the_bridge_is_a_gateway_unit_run_by_the_system_interpreter() -> None:
    bridge = _bridge()
    assert bridge.capabilities == frozenset({"gateway"})
    assert bridge.cmd.startswith("/usr/bin/python3 services/redis_bridge/relay.py")
    assert bridge.identity_probe is not None


def test_a_macos_gateway_with_a_private_address_is_not_gated(
    unit_home: Path, machine_identity: MachineIdentity
) -> None:
    (unit_home / "start-intent.json").write_text(
        json.dumps(
            {
                "record": {
                    "ports": {"redis": 16380},
                    "gateway_home": str(unit_home),
                    "created_at": "t",
                }
            }
        )
    )
    machine_identity.set(host="10.64.0.7")
    endpoint = redis_bridge_endpoint(
        unit_home,
        machine_host=machine_identity.host,
        remote_data_plane=_remote_data_plane,
        platform="darwin",
    )
    assert endpoint == RedisBridgeEndpoint("10.64.0.7", 16380)
    assert gate_reason("redis-bridge", BridgeFact(unconfigured=None, has_endpoint=True)) is None


def test_the_unit_command_carries_the_recorded_listen_address() -> None:
    command, host, port = redis_bridge_launch(RedisBridgeEndpoint("10.64.0.7", 16380))
    assert command == (
        "/usr/bin/python3 services/redis_bridge/relay.py --listen-host 10.64.0.7 "
        "--listen-port 16380 --backend-port 16380"
    )
    assert (host, port) == ("10.64.0.7", 16380)


def test_a_non_macos_host_is_gated_out(unit_home: Path, machine_identity: MachineIdentity) -> None:
    endpoint = redis_bridge_endpoint(
        unit_home,
        machine_host=machine_identity.host,
        remote_data_plane=_remote_data_plane,
        platform="linux",
    )
    assert endpoint is None
    reason = gate_reason("redis-bridge", BridgeFact(unconfigured=None, has_endpoint=False))
    assert reason == "no off-box Redis ingress on this host"


def test_a_host_without_a_relay_keeps_its_bare_declaration() -> None:
    assert redis_bridge_launch(None) == ("/usr/bin/python3 services/redis_bridge/relay.py", "", 0)
