import json
import os
import sys
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV, SERVICE_PLUGIN_CONFIG_ENV
from base.host.env.tests.env_files import unit_env_path
from base.host.proc import run_bounded
from base.packages.plugins.config_registration import (
    disk_image_path,
    plugin_bootstrap_config,
    service_config_packet,
)
from base.packages.plugins.flags import UndeclaredFlag, read_declared_flag
from base.paths import meiva_home
from meiva_builtins.plugins.meiva_fleet.default_config import FleetConfig, contribute
from meiva_builtins.plugins.meiva_fleet.services import services

_CHILD = Path(__file__).with_name("child_plugin_config.py")
_REPO_ROOT = Path(__file__).resolve().parents[4]
_GATEWAY = "http://gateway.invalid:8000"
_ROLE_AND_PACKET_KEYS = (
    PLUGIN_CLUSTER_CONFIG_ENV,
    SERVICE_PLUGIN_CONFIG_ENV,
    "MEIVA_MACHINE_SERVE_GATEWAY",
    "MEIVA_MACHINE_SERVE_AGENT_RUNNER",
    "MEIVA_GATEWAY_URL",
)
# meiva:note
# A unit that serves the gateway owns its own configuration image and never fetches.
_GATEWAY_UNIT = {
    "MEIVA_MACHINE_SERVE_GATEWAY": "true",
    "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
    "MEIVA_GATEWAY_URL": _GATEWAY,
}
# meiva:note
# A configured pure agent-runner takes the cluster fields from the packet its launcher delivers.
_PURE_RUNNER = {
    "MEIVA_CONFIG_FETCH": "skip",
    "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
    "MEIVA_GATEWAY_URL": _GATEWAY,
}


# meiva:note
# Which declared fields are host-scoped, i.e. never travel in the gateway's boot packet: the daemon
# gate and the reminder-window policy are read from the image on the machine that writes.
def _host_fields() -> set[str]:
    from base.config import schema_extra

    return {
        name
        for name, info in FleetConfig.model_fields.items()
        if schema_extra(info).get("scope") not in {"cluster-pinned", "cluster-default"}
    }


@pytest.fixture(autouse=True)
def preserve_env_file() -> Generator[None]:
    env = unit_env_path()
    previous = env.read_bytes() if env.exists() else None
    try:
        yield
    finally:
        if previous is None:
            env.unlink(missing_ok=True)
        else:
            env.write_bytes(previous)


def write_image(config: FleetConfig) -> Path:
    path = disk_image_path("meiva_fleet", home=meiva_home())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(config.model_dump_json())
    return path


# meiva:note
# The outcome of composing the fleet configuration in a fresh process whose environment is this
# test process's, minus the role flags and packets, plus `env`.
def compose_in_child(mode: str, env: dict[str, str]) -> dict[str, Any]:
    child_env = {
        key: value for key, value in os.environ.items() if key not in _ROLE_AND_PACKET_KEYS
    } | env
    proc = run_bounded(
        [sys.executable, "-I", str(_CHILD), mode],
        cwd=_REPO_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_remote_cluster_projection_never_overrides_local_host_gate() -> None:
    write_image(FleetConfig(task_maintenance_enabled=False, task_escalate_n=99))
    gateway = FleetConfig(task_escalate_n=7, reduce_context_switch=False).model_dump(mode="json")
    for name in _host_fields():
        gateway.pop(name)
    composed = compose_in_child(
        "image", _PURE_RUNNER | {PLUGIN_CLUSTER_CONFIG_ENV: json.dumps({"meiva_fleet": gateway})}
    )["config"]
    assert composed["task_maintenance_enabled"] is False
    assert composed["task_escalate_n"] == 7
    assert composed["reduce_context_switch"] is False
    refused = compose_in_child("image", _PURE_RUNNER)
    assert refused["refused"] == "InvalidConfigData"
    assert "lacks cluster" in refused["message"]


def test_gateway_unit_without_a_packet_composes_from_its_local_image() -> None:
    write_image(FleetConfig(task_escalate_n=7, reduce_context_switch=False))
    composed = compose_in_child("image", _GATEWAY_UNIT)["config"]
    assert composed["task_escalate_n"] == 7
    assert composed["reduce_context_switch"] is False


@pytest.mark.parametrize("packet", ['{"meiva_fleet":null}', '{"meiva_fleet":[]}', "[]"])
def test_invalid_present_projection_is_rejected_even_on_gateway(packet: str) -> None:
    write_image(FleetConfig())
    refused = compose_in_child("image", _GATEWAY_UNIT | {PLUGIN_CLUSTER_CONFIG_ENV: packet})
    assert refused["refused"] == "TypeError"


def test_gateway_truth_is_authoritative_and_projection_has_exact_scope() -> None:
    write_image(FleetConfig(task_escalate_n=7, task_maintenance_enabled=False))
    packet = json.loads(plugin_bootstrap_config(home=meiva_home()))["meiva_fleet"]
    assert set(packet) == set(FleetConfig.model_fields) - _host_fields()
    assert packet["task_escalate_n"] == 7

    def deliver(delivered: dict[str, object]) -> dict[str, Any]:
        return compose_in_child(
            "image",
            _GATEWAY_UNIT | {PLUGIN_CLUSTER_CONFIG_ENV: json.dumps({"meiva_fleet": delivered})},
        )

    assert deliver(packet | {"task_escalate_n": 99})["config"]["task_escalate_n"] == 7
    assert deliver(packet | {"task_escalate_n": "invalid"})["refused"] == "InvalidConfigData"
    without_field = {name: value for name, value in packet.items() if name != "task_escalate_n"}
    assert deliver(without_field)["refused"] == "SchemaDriftError"


def test_gate_and_daemon_birth_share_one_captured_image() -> None:
    image = write_image(FleetConfig(task_maintenance_enabled=False, task_escalate_n=7))
    service = services(home=meiva_home())[0]
    assert service.config_inputs == (image,)
    assert service.gate is not None and "disabled" in str(service.gate())
    assert service.plugin_config is not None
    name, captured = service.plugin_config
    write_image(FleetConfig(task_maintenance_enabled=True, task_escalate_n=99))
    actual = compose_in_child(
        "service", {SERVICE_PLUGIN_CONFIG_ENV: service_config_packet(name, captured)}
    )["config"]
    assert actual["task_escalate_n"] == 7
    assert actual["task_maintenance_enabled"] is False
    assert "disabled" in str(service.gate())


def test_core_dependency_is_declared_and_validated_without_sdk_install() -> None:
    assert contribute().flags == ("daemon.notice_ttl_limit_seconds",)
    assert isinstance(read_declared_flag(contribute().flags[0], contribute().flags), float)
    with pytest.raises(UndeclaredFlag):
        read_declared_flag("daemon.notice_ttl_limit_seconds", ())
