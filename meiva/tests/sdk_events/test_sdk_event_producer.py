from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
from base.paths import logs_dir
from base.telemetry import emit
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainStatus, Event
from meiva.sdk_surface import metering, process_context

_CHILD = Path(__file__).with_name("sdk_exit_child.py")
_REPO_ROOT = Path(__file__).resolve().parents[3]


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


# meiva:note
# Every record of the JSONL event mirror under `logs` whose SDK call is `fn`.
def _mirrored_calls(logs: Path, fn: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(logs.glob("events-*.jsonl")):
        for text in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(text)
            if record["attributes"].get("fn") == fn:
                records.append(record)
    return records


# meiva:note
# The first plugin load inside a bound attachment gives the installation its own cold client set
# and leaves the attachment's writer alone: the set is registered to close at process exit, builds
# no writer by being installed, and once the attachment is closed and detached a bare SDK call
# meters through the installation's set, with the system source and no agent.
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
        assert installation is not None
        bare = installation.clients
        assert isinstance(bare, ClientSet) and bare is not attachment
        assert bare.exports_deferred() is None
        attachment.close()
        process_context.unbind_context()
        wrapped = metering.make_recorder(lambda: "done", "probe.detached", sampling, bare)
        assert wrapped() == "done"
        assert bare.sync_events(timeout=5).status is DrainStatus.COMPLETED
        [record] = _mirrored_calls(logs_dir(), "probe.detached")
        assert record["source"] == "system"
        assert record["agent_id"] is None
    finally:
        current = install.uninstall()
        if current is not None and isinstance(current.clients, ClientSet):
            current.clients.close()
        install.clear_load_attempt()
        if prior is not None:
            install.install(
                prior.registry,
                catalog=prior.catalog,
                authority=prior.authority,
                sampling=prior.sampling,
                clock=prior.clock,
                clients=prior.clients,
                config_images=prior.config_images,
            )


# meiva:note
# Run `sdk_exit_child.py` in `mode` with a home of its own and a database nothing listens on, and
# return the mirrored records of its one bare call.
def _bare_call_of_a_child(tmp_path: Path, mode: str) -> list[dict[str, Any]]:
    home = tmp_path / "home"
    home.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env["MEIVA_HOME"] = str(home)
    env["MEIVA_DB_URL"] = f"postgresql://nobody@127.0.0.1:{port}/none"
    subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-X", "utf8", str(_CHILD), str(_REPO_ROOT), mode],
        env=env,
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=True,
    )
    return _mirrored_calls(home / "logs", "probe.exit")


# meiva:note
# The client set a plugin load installs is closed when the process exits normally: its writer is
# stopped then, so a call made just before the exit still reaches the event mirror, with the system
# source and no agent. A hard exit runs no exit hook and loses the same call, which is how the
# first outcome is known to come from the registered hook.
def test_the_installations_clients_are_closed_at_process_exit(tmp_path: Path) -> None:
    [record] = _bare_call_of_a_child(tmp_path, "exit")
    assert record["source"] == "system"
    assert record["agent_id"] is None


def test_a_hard_exit_runs_no_close_hook_and_loses_the_pending_call(tmp_path: Path) -> None:
    assert _bare_call_of_a_child(tmp_path, "hard") == []
