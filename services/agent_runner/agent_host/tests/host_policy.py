import random
from dataclasses import replace
from pathlib import Path
from typing import Any

from base.agents.messages.timestamp_policy import TimestampSource
from base.clock import Clock
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import settings
from base.config.tests.configuration import defaults, snapshot_process_config
from base.native_process.stop_timing import CANCEL_UNWIND_TIMEOUT_S
from base.packages.plugin_providers import build_model_catalog
from base.packages.tests.model_catalog import inert_keys
from services.agent_runner.agent_host.runtime import HostCachePolicy, HostPolicy, StallTiming


# meiva:note
# A host policy with this process's settings as its values, the way the root reads them once;
# `changes` name the fields a test gives other values.
def configured_policy(**changes: Any) -> HostPolicy:
    daemon = settings.daemon
    return replace(
        HostPolicy(
            max_concurrent_turns=daemon.host_max_concurrent_turns,
            provider_keys=inert_keys(build_model_catalog()),
            cache=HostCachePolicy(
                idle_ttl_seconds=daemon.host_agent_idle_ttl_seconds,
                size=daemon.host_agent_cache_size,
            ),
            default_model=settings.lm.llm_model,
            llm_override=settings.lm.llm_override,
            default_reader=defaults(),
            clock_factory=Clock.from_settings,
            harness_registration_ttl_seconds=daemon.harness_registration_ttl_seconds,
            harness_environ={"HOME": "/nonexistent/host-policy-home"},
            rng=random.Random(0),  # noqa: S311
            user_home=Path("/nonexistent/host-policy-user-home"),
            stall=StallTiming(
                scan_interval_s=daemon.host_turn_progress_scan_interval_seconds,
                no_progress_timeout_s=daemon.host_turn_no_progress_timeout_seconds,
                unwind_timeout_s=CANCEL_UNWIND_TIMEOUT_S,
            ),
        ),
        **changes,
    )


def configured_machine(*, name: str | None = None) -> ConfiguredMachineIdentity:
    machine = ConfiguredMachineIdentity(snapshot_process_config())
    if name is not None:
        machine.set(name=name)
    return machine


def assert_sdk_context(expected: object | None, *, bound: bool) -> None:
    import meiva

    assert getattr(meiva, "context", None) is expected
    assert hasattr(meiva, "context") is bound


# meiva:note
# The test composition root supplies the host's independent clock resource explicitly.
def handoff_timestamps() -> TimestampSource:
    return TimestampSource(
        clock=Clock.from_settings(), timestamps_enabled=settings.general.message_timestamps
    )
