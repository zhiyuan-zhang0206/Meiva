from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Any

import pytest

from agent.state import plugin_state_schema
from base import config
from base.agents.context.clients import ClientSet
from meiva.sdk_surface import install, process_context
from meiva.sdk_surface.extensions import load_extensions
from services.agent_runner.agent_host import daemon
from services.agent_runner.agent_host.lifecycle.configuration import load_installation


def _cold_pipeline() -> tuple[list[int], Any]:
    calls: list[int] = []

    def build_pipeline() -> Any:
        calls.append(1)
        raise AssertionError("plugin loading must keep its pipeline cold")

    return calls, build_pipeline


def test_sdk_posture_precedes_config_boot(unit_home: Path) -> None:
    (unit_home / ".env").write_bytes(b"\xff")
    with pytest.raises(RuntimeError, match="established SDK process context"):
        daemon.main()
    assert not process_context.is_host_process()


def test_host_root_supplies_model_and_config_owners() -> None:
    boot = config.ConfigBoot(environment={**os.environ, "MEIVA_CONFIG_FETCH": "skip"})
    boot.boot()
    try:
        calls, pipeline = _cold_pipeline()
        clients = ClientSet(pipeline_factory=pipeline)
        installation = load_installation(
            boot,
            clients=clients,
            load_extensions=partial(load_extensions, validate_state=plugin_state_schema),
        )
        assert installation.require_catalog().models
        assert installation.clients is clients
        assert calls == []
        assert installation.authority is not None
        assert installation.authority.runtime is boot.view
        assert installation.authority.all_domains.profile is None
        assert installation.authority.all_domains is boot.view
    finally:
        install.uninstall()


def test_profiled_host_defers_complete_model_until_first_service_read() -> None:
    _assert_profiled_host_defers_complete_model()


def _assert_profiled_host_defers_complete_model() -> None:
    env = {
        **os.environ,
        "MEIVA_CONFIG_FETCH": "skip",
        "MEIVA_PROCESS_PROFILE": "agent",
        "MEIVA_PROVIDER_GUARD_BALANCE_MIN_CNY": "not-a-float",
    }
    boot = config.ConfigBoot(environment=env)
    boot.boot()
    runtime = boot.view
    try:
        calls, pipeline = _cold_pipeline()
        installation = load_installation(
            boot,
            clients=ClientSet(pipeline_factory=pipeline),
            load_extensions=partial(load_extensions, validate_state=plugin_state_schema),
        )
        assert calls == []
        authority = installation.authority
        assert authority is not None
        assert authority.runtime is runtime
        assert authority.service_field_value("machine_host") == runtime.general.machine_host
        env["MEIVA_PROVIDER_GUARD_BALANCE_MIN_CNY"] = "42.5"
        first = authority.service_field_value("provider_guard_balance_min_cny")
        assert first == 42.5
        env["MEIVA_PROVIDER_GUARD_BALANCE_MIN_CNY"] = "99.0"
        assert authority.service_field_value("provider_guard_balance_min_cny") == first
    finally:
        install.uninstall()
