# meiva:note
# The host root supplies its configuration authority and producer without eagerly constructing a complete Settings view for a profile-restricted process.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/configuration.meiva.okf.md

from collections.abc import Callable
from pathlib import Path

from base import paths
from base.agents.context.clients import ClientSet
from base.agents.history.hierarchy.group_consumer import UnderstandingReadInputs
from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.lm.catalog.provider_keys import ProviderKeys
from base.native_process.loaded_commit import LoadedCommit
from base.native_process.stop_timing import CANCEL_UNWIND_TIMEOUT_S
from base.paths.home import resolve_meiva_home
from base.telemetry.process_logging import init_gateway_process
from meiva.boot import process_disable_entries, process_sampling_owner
from meiva.sdk_surface.installation import Installation
from services.agent_runner.agent_host.pools import PoolBounds
from services.agent_runner.agent_host.runtime import HostCachePolicy, HostPolicy, StallTiming

__all__ = [
    "boot_process",
    "host_machine",
    "host_policy",
    "initialize_logging",
    "load_installation",
    "pool_bounds",
    "understanding_inputs",
]


def boot_process() -> tuple[ConfigBoot, Path]:
    config = ConfigBoot()
    config.boot()
    config.ensure_eager()
    home = resolve_meiva_home()
    return config, home


def host_policy(config: ConfigBoot, *, keys: ProviderKeys) -> HostPolicy:
    daemon = config.view.daemon
    return HostPolicy(
        max_concurrent_turns=daemon.host_max_concurrent_turns,
        provider_keys=keys,
        cache=HostCachePolicy(
            idle_ttl_seconds=daemon.host_agent_idle_ttl_seconds, size=daemon.host_agent_cache_size
        ),
        default_model=config.view.lm.llm_model,
        llm_override=config.view.lm.llm_override,
        default_reader=lambda domain, field: getattr(getattr(config.view, domain), field),
        clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        handoff_notes=TimestampPolicy(
            clock=Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
            timestamps_enabled=config.view.general.message_timestamps,
        ),
        harness_registration_ttl_seconds=daemon.harness_registration_ttl_seconds,
        stall=StallTiming(
            scan_interval_s=daemon.host_turn_progress_scan_interval_seconds,
            no_progress_timeout_s=daemon.host_turn_no_progress_timeout_seconds,
            unwind_timeout_s=CANCEL_UNWIND_TIMEOUT_S,
        ),
    )


def pool_bounds(config: ConfigBoot) -> PoolBounds:
    return PoolBounds(
        workload_max_size=config.view.daemon.host_db_pool_max_size,
        control_max_size=config.view.daemon.host_control_pool_max_size,
        acquire_timeout_s=config.view.agent.db_pool_acquire_timeout_seconds,
        slow_acquire_warn_s=config.view.agent.db_pool_slow_acquire_warn_seconds,
    )


def understanding_inputs(config: ConfigBoot) -> UnderstandingReadInputs:
    return UnderstandingReadInputs(
        enabled=lambda: config.view.agent.understanding_enabled,
        default_model=lambda: config.view.lm.llm_model,
        hierarchy_model=lambda: config.view.lm.hierarchy_model,
        group_model=lambda: config.view.agent.understanding_group_model,
        check_open=lambda: config.view.agent.understanding_group_check_open,
        check_decay=lambda: config.view.agent.understanding_group_check_decay,
        reasoning=lambda: config.view.agent.understanding_group_reasoning,
        corrections=lambda: config.view.agent.understanding_group_corrections,
        clock_factory=lambda: Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        timestamps_enabled=lambda: config.view.general.message_timestamps,
    )


def host_machine(config: ConfigBoot) -> MachineIdentity:
    return machine_from_boot(config)


def load_installation(
    config: ConfigBoot,
    *,
    clients: ClientSet,
    load_extensions: Callable[..., object],
) -> Installation:
    from base.config import Settings
    from base.config.service_read import ConfigAuthority
    from base.lm.factory import process_provider_keys
    from base.packages.plugin_providers import build_model_catalog
    from meiva.sdk_surface.installation import installed

    env_path = paths.meiva_home() / ".env"
    if config.view.profile is None:
        authority = ConfigAuthority(runtime=config.view, all_domains=config.view, env_path=env_path)
    else:
        authority = ConfigAuthority.deferred(
            runtime=config.view, build_all_domains=lambda: Settings(profile=None), env_path=env_path
        )
    catalog = build_model_catalog()
    load_extensions(
        catalog=catalog,
        provider_keys=process_provider_keys(catalog),
        authority=authority,
        clients=clients,
        sampling=process_sampling_owner(),
        clock=Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS),
        disabled_entries=process_disable_entries(),
    )
    installation = installed()
    if installation is None:
        raise RuntimeError("the plugin load did not install its SDK surface")
    return installation


def initialize_logging(clients: ClientSet, machine: MachineIdentity, image: LoadedCommit) -> None:
    try:
        init_gateway_process(
            name="agent_host",
            producer=clients.event_pipeline,
            machine_reader=machine.name,
            image=image,
        )
    except BaseException as primary:
        try:
            clients.close()
        except BaseException as secondary:
            primary.add_note(f"Host logging cleanup also failed: {secondary!r}")
        raise
