from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.agents.sdk import call_policy
from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner, sampling_owner_serving
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.telemetry import emit
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainStatus, Event
from meiva.sdk_surface import metering, process_context


@pytest.fixture
def sampling() -> Iterator[call_policy.SamplingPolicyOwner]:
    owner = fixed_sampling_owner()
    yield owner
    owner.close()


@pytest.fixture
def bare_context() -> Iterator[None]:
    prior = getattr(meiva, "context", None)
    process_context.unbind_context()
    try:
        yield
    finally:
        if prior is None:
            process_context.unbind_context()
        else:
            process_context.bind_context(prior)


def test_bare_python_recorder_uses_its_captured_cold_producer(
    bare_context: None, sampling: call_policy.SamplingPolicyOwner
) -> None:
    order: list[str] = []
    written: list[Event] = []
    built: list[EventPipeline] = []

    def producer() -> EventPipeline:
        order.append("producer")
        pipe = EventPipeline(writer=written.extend, batch_size=1)
        built.append(pipe)
        return pipe

    def body() -> str:
        order.append("body")
        return "done"

    wrapped = metering.make_recorder(
        body, "probe.bare", sampling, ClientSet(pipeline_factory=producer)
    )
    assert order == [] and built == []
    try:
        assert wrapped() == "done"
        assert order == ["body", "producer"]
        assert built[0].sync(timeout=1).status is DrainStatus.COMPLETED
        assert written[0].source == "system" and written[0].agent_id is None
        assert written[0].attributes["fn"] == "probe.bare"
    finally:
        for pipe in built:
            assert pipe.stop(timeout=1).status is DrainStatus.COMPLETED


def test_sampled_out_call_does_not_construct_a_producer(bare_context: None, tmp_path: Path) -> None:
    owner = sampling_owner_serving(
        call_policy.SamplingPolicy(sampling_enabled=True, sample_every=1_000_000_000),
        tmp_path / ".env",
    )

    def forbidden() -> EventPipeline:
        pytest.fail("sampled-out SDK call built a writer")

    wrapped = metering.make_recorder(
        lambda: "done", "probe.sample", owner, ClientSet(pipeline_factory=forbidden)
    )
    try:
        assert wrapped() == "done"
    finally:
        owner.close()


def test_capture_failure_prevents_lazy_producer_construction() -> None:
    original = ValueError("durable capture failed")

    def capture(event: Event) -> Event:
        raise original

    def forbidden() -> EventPipeline:
        pytest.fail("producer was constructed before durable capture")

    with pytest.raises(ValueError) as observed:
        emit(
            "telemetry", "sdk_call", attributes={"fn": "probe"}, capture=capture, producer=forbidden
        )
    assert observed.value is original


def test_context_switch_in_body_does_not_change_the_captured_producer_or_identity(
    bare_context: None,
    sampling: call_policy.SamplingPolicyOwner,
    process_config: ConfigBoot,
) -> None:
    written: list[Event] = []
    built: list[EventPipeline] = []

    def producer() -> EventPipeline:
        pipe = EventPipeline(writer=written.extend, batch_size=1)
        built.append(pipe)
        return pipe

    def forbidden() -> EventPipeline:
        pytest.fail("the call switched its admitted producer to the new context")

    first = ClientSet(pipeline_factory=producer)
    second = ClientSet(pipeline_factory=forbidden)
    first_machine = machine_from_boot(process_config)
    second_machine = machine_from_boot(process_config)
    first_machine.set(name="first-sdk-owner")
    second_machine.set(name="second-sdk-owner")
    old_context = MeivaContext(
        identity=AgentIdentity(6111, False), clients=first, machine=first_machine
    )
    new_context = MeivaContext(
        identity=AgentIdentity(6112, False), clients=second, machine=second_machine
    )
    process_context.bind_context(old_context)

    def body() -> None:
        process_context.bind_context(new_context)

    wrapped = metering.make_recorder(
        body, "probe.switch", sampling, ClientSet(pipeline_factory=forbidden)
    )
    try:
        wrapped()
        assert first.sync_events(timeout=1).status is DrainStatus.COMPLETED
        assert len(built) == 1 and len(written) == 1
        assert written[0].agent_id == 6111 and written[0].source == "agent:6111"
        assert written[0].machine == "first-sdk-owner"
    finally:
        first.close(pipeline_timeout=1)
        second.close(pipeline_timeout=1)


def test_first_bound_plugin_load_does_not_lend_attachment_writer_to_bare_calls(
    bare_context: None,
    sampling: call_policy.SamplingPolicyOwner,
    process_config: ConfigBoot,
    machine_identity: MachineIdentity,
) -> None:
    from meiva.sdk_surface import install
    from meiva.sdk_surface.installation import installed

    prior = install.uninstall()
    install.clear_load_attempt()
    attachment = ClientSet(pipeline_factory=lambda: pytest.fail("closed attachment was borrowed"))
    process_context.bind_context(
        MeivaContext(clients=attachment, identity=AgentIdentity(agent_id=6111, owns_loop=False))
    )
    try:
        meiva.ensure_plugins_loaded(config=process_config, machine=machine_identity)
        installation = installed()
        assert installation is not None and installation.clients is not None
        assert installation.clients is not attachment
        attachment.close()
        process_context.unbind_context()
        written: list[Event] = []
        wrapped = metering.make_recorder(
            lambda: "done",
            "probe.detached",
            sampling,
            ClientSet(pipeline_factory=lambda: EventPipeline(writer=written.extend, batch_size=1)),
        )
        assert wrapped() == "done"
    finally:
        current = install.uninstall()
        if current is not None and current.clients is not None:
            cast(ClientSet, current.clients).close()
        install.clear_load_attempt()
        if prior is not None:
            install.install(
                prior.registry,
                catalog=prior.catalog,
                authority=prior.authority,
                sampling=prior.require_sampling(),
                clock=prior.clock,
                clients=prior.clients,
                config_images=prior.config_images,
            )
