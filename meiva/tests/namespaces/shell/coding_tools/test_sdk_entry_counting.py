from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import cast

import httpx

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.agents.sdk import call_policy
from base.agents.sdk.tally import SdkCallTally
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions, SdkMember
from base.paths.home import resolve_meiva_home
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.sdk_surface import install, process_context
from meiva.shell.coding_tools import owner_terminated


# meiva:note
# The shell's owner probe is registered as a plugin member and calls the real, metered
# `meiva.agents.get_status` inside it: each public entry counts once. The gateway is down, which
# the probe reads as "the owner is alive" (an unavailable gateway cannot prove an owner dead).
def test_real_shell_owner_probe_counts_its_public_fanout(emitted_events: EmittedEvents) -> None:
    def gateway_down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway down", request=request)

    transport = GatewayTransportInputs(
        max_retries_reader=lambda: 1,
        retry_delay_reader=lambda: 0.0,
        memory_deadline_reader=lambda: 1.0,
    )
    sampling = call_policy.SamplingPolicyOwner(reader=call_policy.SamplingPolicy)
    registry = ExtensionRegistry(
        (
            (
                "probe",
                PluginContributions(
                    sdk_members=(SdkMember("self", "owner_probe_test", owner_terminated),),
                ),
            ),
        )
    )
    previous = getattr(meiva, "context", None)
    tally = SdkCallTally()
    with httpx.Client(
        base_url="http://gateway.test", transport=httpx.MockTransport(gateway_down)
    ) as gateway:
        clients = ClientSet(
            gateway_url="http://gateway.test",
            gateway=lambda _url: gateway,
            pipeline_factory=emitted_events.producer,
            factories={GatewayTransportInputs: lambda: transport},
        )
        install.install(
            registry,
            sampling=sampling,
            config_images=DiskPluginConfigImages(resolve_meiva_home()),
        )
        process_context.bind_context(
            replace(previous or MeivaContext(), sdk_calls=tally, clients=clients)
        )
        try:
            assert not cast(Callable[[int], bool], meiva.self.owner_probe_test)(41)
        finally:
            if previous is None:
                process_context.unbind_context()
            else:
                process_context.bind_context(previous)
            install.uninstall()
            sampling.close()
    assert tally.snapshot() == {"agents.get_status": 1, "self.owner_probe_test": 1}
    assert [event.attributes["fn"] for event in emitted_events] == [
        "agents.get_status",
        "self.owner_probe_test",
    ]
