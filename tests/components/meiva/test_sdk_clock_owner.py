from __future__ import annotations

from collections.abc import Iterator

import pytest

from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry
from base.paths.home import resolve_meiva_home
from meiva.sdk_surface import install, process_context, settings
from meiva.sdk_surface.tests.installation_holder import inert_sampling


@pytest.fixture
def clock_scope() -> Iterator[None]:
    prior_context = process_context.unbind_context()
    prior = install.uninstall()
    try:
        yield
    finally:
        current = install.uninstall()
        if current is not None:
            current.sampling.close()
        if prior is not None:
            install.install(
                prior.registry,
                catalog=prior.catalog,
                provider_keys=prior.provider_keys,
                authority=prior.authority,
                sampling=prior.sampling,
                clock=prior.clock,
                clients=prior.clients,
                config_images=prior.config_images,
            )
        if prior_context is not None:
            process_context.bind_context(prior_context)


def _clock(timezone: str) -> Clock:
    config = ClockConfig(
        timezone=timezone, authoritative_timezone=timezone, message_timestamp_weekday=False
    )
    return Clock(config, timers=SYSTEM_TIMERS)


def test_bare_sdk_clock_is_the_installation_clock(clock_scope: None) -> None:
    clock = _clock("UTC")

    install.install(
        ExtensionRegistry(()),
        clock=clock,
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=inert_sampling(),
    )

    assert settings.clock() is clock
    assert settings.clock().authoritative_timezone == "UTC"


def test_bound_sdk_clock_uses_context_without_borrowing_installation(clock_scope: None) -> None:
    install.install(
        ExtensionRegistry(()),
        clock=_clock("Asia/Shanghai"),
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=inert_sampling(),
    )
    context_clock = _clock("UTC")
    process_context.bind_context(
        MeivaContext(clients=ClientSet(), clock_factory=lambda: context_clock)
    )
    try:
        assert settings.clock() is context_clock
    finally:
        process_context.unbind_context()


def test_partial_context_refuses_clock_instead_of_falling_back(clock_scope: None) -> None:
    install.install(
        ExtensionRegistry(()),
        clock=_clock("UTC"),
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=inert_sampling(),
    )
    process_context.bind_context(MeivaContext(clients=ClientSet()))
    try:
        with pytest.raises(RuntimeError, match="clock"):
            settings.clock()
    finally:
        process_context.unbind_context()


def test_inventory_installation_remains_cold_and_refuses_clock(clock_scope: None) -> None:
    install.install(
        ExtensionRegistry(()),
        config_images=DiskPluginConfigImages(resolve_meiva_home()),
        sampling=inert_sampling(),
    )
    with pytest.raises(RuntimeError, match="carries no Clock"):
        settings.clock()
