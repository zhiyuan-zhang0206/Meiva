# meiva:note
# Unit tests for meiva/sdk_surface/metering.py — the per-call SDK usage recorder.
#
# The recorder wraps every public `meiva.*` callable to emit one `sdk_call` event per public
# entry (summed by the Grafana call-frequency ranking). These tests pin the two things that make
# it safe to bolt onto the whole SDK surface: it is byte-for-byte transparent to `meiva.help` /
# signatures, and records each entry independently. Emitter failures preserve an already-raised
# body exception as the primary outcome.
#
# Events are read from the real emitter's capture (`emitted_events`); the recorder takes the
# client set it meters through, and a sampling owner whose reader returns a valid policy.


from __future__ import annotations

import asyncio
import contextlib
import inspect
import io
from collections.abc import Callable, Generator, Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import cast

import pytest

import meiva
from base.agents.context import MeivaContext
from base.agents.context.clients import ClientSet
from base.agents.context.identity import ExternalLease
from base.agents.sdk import call_policy
from base.agents.sdk.tally import SdkCallTally
from base.agents.sdk.tests.sampling_owners import (
    fixed_sampling_owner,
    sampling_owner_failing_to_read,
)
from base.api_contracts.caller_identity import CallerIdentity
from base.cluster.machine import MachineFacts
from base.packages.plugin_config_images import DiskPluginConfigImages
from base.packages.plugins.extensions import (
    ExtensionRegistry,
    PluginContributions,
    SdkMember,
    SdkWrap,
)
from base.paths.home import resolve_meiva_home
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events
from meiva.sdk_surface import install, meiva_module, metering, process_context
from meiva.sdk_surface.installation import Installation
from meiva.sdk_surface.tests.installation_holder import held_installation
from meiva.tests.fixtures.pin_agent import pin_agent


@pytest.fixture
def sampling_owner() -> Iterator[call_policy.SamplingPolicyOwner]:
    owner = fixed_sampling_owner()
    yield owner
    owner.close()


@pytest.fixture
def capture(emitted_events: EmittedEvents) -> Iterator[ClientSet]:
    """Bind a context whose clients' event pipeline is the capture, and put the previous one back."""
    clients = ClientSet(pipeline_factory=emitted_events.producer)
    previous = process_context.unbind_context()
    process_context.bind_context(MeivaContext(clients=clients))
    try:
        yield clients
    finally:
        process_context.unbind_context()
        if previous is not None:
            process_context.bind_context(previous)


@contextlib.contextmanager
def _execution_tally() -> Generator[SdkCallTally, None, None]:
    """A test execution explicitly owns its SDK tally through the local context."""
    previous = meiva.context
    tally = SdkCallTally()
    process_context.bind_context(replace(previous, sdk_calls=tally))
    try:
        yield tally
    finally:
        process_context.bind_context(previous)


# meiva:note
# (fn, duration) of every `sdk_call` event emitted so far, in emit order.
def _calls(events: EmittedEvents) -> list[tuple[str, float | None]]:
    return [
        (row["fn"], row.get("duration"))
        for row in events.attributes("sdk_call")
        if isinstance(row.get("fn"), str)
    ]


def _install_recorders(
    sampling: call_policy.SamplingPolicyOwner, root: ModuleType | None = None
) -> tuple[tuple[object, str], ...]:
    surface = metering.read_surface(root if root is not None else meiva_module())
    return metering.install(sampling, clients=None, surface=surface)


def _help(*targets: object) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        meiva.help(*targets)
    return buf.getvalue()


@pytest.fixture
def _installed(sampling_owner: call_policy.SamplingPolicyOwner) -> Iterator[None]:
    """Install the recorders over the real `meiva` singleton, then restore — so a
    wrapped function never leaks into the rest of the suite."""
    ledger = _install_recorders(sampling_owner)
    try:
        yield
    finally:
        metering.uninstall(ledger)


# ── transparency ──────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("sdk_model_owner")
def test_help_is_byte_identical_across_install(
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    """Acceptance for the transparency contract: metering must not change a single
    byte of what the agent sees via `meiva.help`."""
    before_root = _help(meiva)
    before_ns = _help(meiva.files)
    before_fn = _help(meiva.files.read)

    ledger = _install_recorders(sampling_owner)
    try:
        assert _help(meiva) == before_root
        assert _help(meiva.files) == before_ns
        assert _help(meiva.files.read) == before_fn
    finally:
        metering.uninstall(ledger)


def test_signature_and_identity_metadata_preserved(
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    before_sig = inspect.signature(meiva.files.read)
    before_doc = meiva.files.read.__doc__
    ledger = _install_recorders(sampling_owner)
    try:
        read = meiva.files.read
        assert read.__name__ == "read"
        assert read.__module__ == "meiva.files"
        assert read.__doc__ == before_doc
        assert inspect.signature(read) == before_sig
    finally:
        metering.uninstall(ledger)


def test_function_attached_members_survive(_installed: None) -> None:
    # meiva.understand carries UnderstandError as a function attribute; the __dict__
    # copy in functools.wraps must keep it reachable after wrapping.
    assert isinstance(getattr(meiva.understand, "UnderstandError", None), type)


def test_install_is_idempotent(
    _installed: None, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    once = meiva.files.read
    _install_recorders(sampling_owner)  # second install must not double-wrap
    assert meiva.files.read is once


def test_install_rejects_a_different_owner_before_wrapping(_installed: None) -> None:
    once = meiva.files.read
    other = fixed_sampling_owner()
    try:
        with pytest.raises(ValueError, match="another sampling owner"):
            _install_recorders(other)
    finally:
        other.close()
    assert meiva.files.read is once


# ── enumeration ───────────────────────────────────────────────────────────────


def test_instrument_targets_selects_routines_not_classes_or_constants() -> None:
    fqs = {fq for _parent, _attr, fq in metering.read_surface(meiva_module()).routines}
    # plain functions, nested-namespace functions, and top-level functions
    assert {"files.read", "shell.run", "shell.sessions.new", "self.compact", "understand"} <= fqs
    # meiva.mcps declares its helpers (and the configured servers) in __all_for_meiva__; the
    # helpers are metered here, while dynamic tool calls are metered at the call funnel.
    assert {"mcps.servers", "mcps.description"} <= fqs
    # classes and constants exposed in __all_for_meiva__ are never wrapped
    assert "agents.AgentRow" not in fqs  # a class
    assert "self.AGENT_ID" not in fqs  # a constant
    assert "memory.PATH" not in fqs  # a constant
    # meiva.skills' __all_for_meiva__ is the live skill index only. Its entries are
    # served by module __getattr__ and the static walk resolves none of them, so
    # they are never wrapped (skill use is attributed by skill_invoked events
    # instead). Dynamic MCP server proxies are never recursed into either.
    assert not any(fq.startswith("skills.") for fq in fqs)
    assert not any(fq.startswith("mcps.") and fq.count(".") > 1 for fq in fqs)


def test_instrument_targets_does_not_evaluate_raising_dynamic_member() -> None:
    """Regression: meiva.self.MACHINE_SPEC is served via module
    __getattr__ that computes machine identity and raises MachineNameMissing when unset
    (CI / isolated $MEIVA_HOME / schedule runner). The walk resolves members statically, so
    it never force-evaluates them — otherwise install() crashes load_extensions in the
    child (rc=1)."""
    import base.cluster.machine

    machine = MachineFacts.of(name="", role=(), host="", description=None)
    prior = process_context.unbind_context()
    process_context.bind_context(MeivaContext(machine=machine))
    try:
        # sanity: normal attribute access really does raise under this condition
        with pytest.raises(base.cluster.machine.MachineNameMissing):
            _ = meiva.self.MACHINE_SPEC

        fqs = {fq for _parent, _attr, fq in metering.read_surface(meiva_module()).routines}
        assert "self.compact" in fqs  # real functions still enumerated
        assert "self.MACHINE_SPEC" not in fqs  # dynamic constant skipped, not evaluated
    finally:
        process_context.unbind_context()
        if prior is not None:
            process_context.bind_context(prior)


def test_instrument_targets_does_not_evaluate_a_dynamic_namespace_directory() -> None:
    class Dynamic(ModuleType):
        def __dir__(self) -> list[str]:
            pytest.fail("instrumentation must not load skills or discover remote MCP servers")

    def inner() -> str:
        return "inner"

    def top() -> str:
        return "top"

    dynamic = Dynamic("probe_root.dynamic")
    dynamic.__dict__.update(inner=inner, __all_for_meiva__=["inner"])
    root = ModuleType("probe_root")
    root.__dict__.update(top=top, dynamic=dynamic, __all_for_meiva__=["top", "dynamic"])

    assert {fq for _parent, _attr, fq in metering.read_surface(root).routines} == {
        "top",
        "dynamic.inner",
    }


# ── recorder wrapping (agent side; call / emit logic is in test_sdk_telemetry) ───


def test_plugin_wrapped_signature_survives_and_counts_once(
    sampling_owner: call_policy.SamplingPolicyOwner,
    capture: ClientSet,
    emitted_events: EmittedEvents,
) -> None:
    """A member already wrapped by a plugin (extra kwarg, custom __signature__) stays
    transparent under the recorder and still emits exactly one event."""

    def core(a: int, b: int) -> tuple[int, int]:
        return (a, b)

    def plugin_wrapped(a: int, b: int, *, label: str | None = None) -> tuple[int, int]:
        return core(a, b)

    # mimic meiva.sdk_surface.wraps._install_metadata: identity of the wrapped member + a
    # signature that advertises the plugin's added `label` kwarg.
    plugin_wrapped.__name__ = "spawn"
    plugin_wrapped.__module__ = "meiva.agents"
    plugin_wrapped.__signature__ = inspect.signature(plugin_wrapped)  # type: ignore[attr-defined]

    rec = metering.make_recorder(plugin_wrapped, "agents.spawn", sampling_owner, capture)
    assert rec.__name__ == "spawn"
    assert rec.__module__ == "meiva.agents"
    assert "label" in inspect.signature(rec).parameters
    with _execution_tally():
        assert rec(1, 2, label="x") == (1, 2)
    [(fn, duration)] = _calls(emitted_events)
    assert fn == "agents.spawn"
    assert duration is not None and duration >= 0


def test_recorder_feeds_the_execution_tally(
    sampling_owner: call_policy.SamplingPolicyOwner, capture: ClientSet
) -> None:
    """The wrapped surface bumps the execution's full tally (two calls count two),
    independent of the emit sampler."""
    rec = metering.make_recorder(lambda: "ok", "ns.fn", sampling_owner, capture)
    with _execution_tally() as tally:
        assert rec() == "ok"
        assert rec() == "ok"
    assert tally.snapshot() == {"ns.fn": 2}


def test_recorder_recognized_by_identity_not_copied_dict(
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    """P3: meiva.extend._install_metadata copies a wrapped callable's __dict__ onto its
    wrapper, so a plugin wrapper built over a recorder inherits the recorder's dict.
    is_recorder() must key off object identity (the marker points at the recorder itself), not the attribute's presence, or
    it would skip re-wrapping such a wrapper and leave the recorder buried inside."""
    rec = metering.make_recorder(lambda: None, "ns.fn", sampling_owner, None)
    assert metering.is_recorder(rec)

    def plugin_wrapper() -> None:
        return rec()

    # replicate _install_metadata's `chained.__dict__.setdefault(k, v)` copy.
    for k, v in rec.__dict__.items():
        plugin_wrapper.__dict__.setdefault(k, v)
    assert not metering.is_recorder(plugin_wrapper)


def test_mcp_recorder_derives_fq_from_runtime_args(
    sampling_owner: call_policy.SamplingPolicyOwner,
    capture: ClientSet,
    emitted_events: EmittedEvents,
) -> None:
    """MCP tools are dynamic, so the funnel recorder builds the fq from server/tool at
    call time, both inside and outside an execution tally."""

    def _fake_call(source: object, server: str, tool: str, **_kw: object) -> dict[str, str]:
        return {"server": server, "tool": tool}

    rec = metering.make_mcp_recorder(_fake_call, sampling_owner, capture)
    with _execution_tally():
        assert rec(None, "chrome", "navigate", url="x") == {"server": "chrome", "tool": "navigate"}
    [(fn, duration)] = _calls(emitted_events)
    assert fn == "mcps.chrome.navigate"
    assert duration is not None and duration >= 0

    emitted_events.clear()
    rec(None, "chrome", "navigate")  # without an execution tally
    assert [fn for fn, _duration in _calls(emitted_events)] == ["mcps.chrome.navigate"]


# meiva:note
# Task #3426: teardown restores from the install() ledger, not a fresh namespace walk — the walk
# re-resolves dynamic member surfaces (the `meiva.skills` index scans the skills tree and reads
# the install registry), which state a passing test arranged can poison after the test itself
# went green. The surface is a value `install` takes, and `uninstall` receives only the ledger
# it returned.
def test_uninstall_restores_from_the_install_record_without_a_namespace_walk(
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:

    def demo() -> str:
        return "ok"

    target = SimpleNamespace(demo=demo)
    surface = metering.MeterSurface(routines=((target, "demo", "demo"),), funnel_module=None)
    ledger = metering.install(sampling_owner, clients=None, surface=surface)
    try:
        assert target.demo is not demo
        assert metering.is_recorder(target.demo)
    finally:
        metering.uninstall(ledger)
    assert target.demo is demo


def test_install_meters_the_funnel_module_it_is_given(
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    def call_raw(source: object, server: str, tool: str) -> str:
        return f"{server}.{tool}"

    funnel = SimpleNamespace(call_raw=call_raw)
    surface = metering.MeterSurface(routines=(), funnel_module=funnel)
    ledger = metering.install(sampling_owner, clients=None, surface=surface)
    try:
        assert ledger == ((funnel, "call_raw"),)
        assert metering.is_recorder(funnel.call_raw)
    finally:
        metering.uninstall(ledger)
    assert funnel.call_raw is call_raw


@pytest.mark.asyncio
async def test_async_calls_measure_each_concurrent_entry(
    sampling_owner: call_policy.SamplingPolicyOwner,
    capture: ClientSet,
    emitted_events: EmittedEvents,
) -> None:
    async def body(label: str) -> str:
        await asyncio.sleep(0)
        return label

    wrapped = metering.make_recorder(body, "plugin.async_call", sampling_owner, capture)
    assert inspect.iscoroutinefunction(wrapped)
    a, b = wrapped("a"), wrapped("b")
    assert _calls(emitted_events) == []
    assert await asyncio.gather(a, b) == ["a", "b"]
    calls = _calls(emitted_events)
    assert [fn for fn, _duration in calls] == ["plugin.async_call", "plugin.async_call"]
    assert all(duration is not None for _fn, duration in calls)


@pytest.mark.parametrize("async_call", [False, True])
async def test_recorder_rejects_invalid_sampling_before_the_original_call(
    async_call: bool, tmp_path: Path
) -> None:
    calls: list[str] = []

    def body() -> str:
        calls.append("side effect")
        return "ok"

    async def async_body() -> str:
        return body()

    owner = sampling_owner_failing_to_read(tmp_path / ".env")
    owner.refresh()
    original = async_body if async_call else body
    wrapped = metering.make_recorder(original, "plugin.write", owner, None)
    assert inspect.signature(wrapped) == inspect.signature(original)
    with pytest.raises(UnicodeDecodeError):
        res = wrapped()
        if inspect.isawaitable(res):
            await res
    assert calls == []
    with pytest.raises(UnicodeDecodeError):
        owner.close()


def test_borrowed_identity_is_stamped_on_external_sdk_events(
    sampling_owner: call_policy.SamplingPolicyOwner,
    emitted_events: EmittedEvents,
) -> None:
    def validate() -> int:
        pytest.fail("observational telemetry must not validate the lease")

    pin_agent(
        42,
        lease=ExternalLease(agent_id=99, validate=validate, config=lambda: None),
        clients=ClientSet(pipeline_factory=emitted_events.producer),
    )
    wrapped = metering.make_recorder(lambda: "ok", "files.read", sampling_owner, None)
    assert wrapped() == "ok"
    [event] = emitted_events
    assert event.agent_id == 99
    assert event.source == "agent:99"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_wrapper", [False, True])
async def test_plugin_wrap_preserves_awaited_single_event(
    async_wrapper: bool,
    sampling_owner: call_policy.SamplingPolicyOwner,
    capture: ClientSet,
    emitted_events: EmittedEvents,
) -> None:
    from collections.abc import Awaitable

    async def body() -> str:
        await asyncio.sleep(0.05)
        return "ok"

    def passthrough(inner: Callable[[], Awaitable[str]]) -> Awaitable[str]:
        return inner()

    async def awaited(inner: Callable[[], Awaitable[str]]) -> str:
        return await inner()

    registry = ExtensionRegistry(
        (
            (
                "async-test",
                PluginContributions(
                    sdk_members=(SdkMember("self", "review_async_test", body),),
                    sdk_wraps=(
                        SdkWrap(
                            "self.review_async_test", awaited if async_wrapper else passthrough
                        ),
                    ),
                ),
            ),
        )
    )
    try:
        install.install(
            registry,
            config_images=DiskPluginConfigImages(resolve_meiva_home()),
            sampling=sampling_owner,
        )  # the recorder goes on last, over the plugin's wrap
        call = meiva.self.review_async_test
        assert inspect.iscoroutinefunction(call)
        emitted_events.clear()
        pending = call()
        assert _calls(emitted_events) == []
        assert await pending == "ok"
        [(fn, duration)] = _calls(emitted_events)
        assert fn == "self.review_async_test"
        assert duration is not None and duration >= 0.04
    finally:
        install.uninstall()


# meiva:note
# The external tool profile the installation was built with names who is calling when no
# context does; the recorder stamps it as the event's source. A profile the SDK cannot parse
# never gets this far: the plugin load that reads it fails (see test_external_caller_meiva.py).
def test_the_installations_external_profile_is_stamped_on_sdk_events(
    sampling_owner: call_policy.SamplingPolicyOwner,
    emitted_events: EmittedEvents,
) -> None:
    pin_agent(42, clients=ClientSet(pipeline_factory=emitted_events.producer))
    profile = CallerIdentity(kind="external_agent", subject="codex")
    installation = replace(Installation.bare(sampling=sampling_owner), external_caller=profile)
    wrapped = metering.make_recorder(lambda: "ok", "files.read", sampling_owner, None)
    assert wrapped() == "ok"
    with held_installation(installation):
        assert wrapped() == "ok"
    [without, with_profile] = emitted_events
    assert (without.agent_id, without.source) == (42, "agent:42")
    assert (with_profile.agent_id, with_profile.source) == (42, "external_agent:codex")


async def test_awaited_calls_retain_their_entry_identity(
    sampling_owner: call_policy.SamplingPolicyOwner,
    emitted_events: EmittedEvents,
) -> None:
    """A later attachment cannot relabel an already-admitted async SDK call."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held() -> None:
        entered.set()
        await release.wait()

    first_machine = MachineFacts.of(name="first", role=frozenset(), host="", description=None)
    second_machine = MachineFacts.of(name="second", role=frozenset(), host="", description=None)
    clients = ClientSet(pipeline_factory=emitted_events.producer)
    pin_agent(41, machine=first_machine, clients=clients)
    first_tally = SdkCallTally()
    process_context.bind_context(replace(meiva.context, sdk_calls=first_tally))
    first = asyncio.create_task(
        metering.make_recorder(held, "probe.held", sampling_owner, clients)()
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        pin_agent(42, machine=second_machine, clients=clients)
        second_tally = SdkCallTally()
        process_context.bind_context(replace(meiva.context, sdk_calls=second_tally))
        metering.make_recorder(lambda: None, "probe.next", sampling_owner, clients)()
        release.set()
        await first
    finally:
        release.set()
        await first
    assert [event.machine for event in emitted_events] == ["second", "first"]
    assert first_tally.snapshot() == {"probe.held": 1}
    assert second_tally.snapshot() == {"probe.next": 1}
    assert [(event.attributes["fn"], event.agent_id, event.source) for event in emitted_events] == [
        ("probe.next", 42, "agent:42"),
        ("probe.held", 41, "agent:41"),
    ]


def test_public_fanout_counts_each_entry(
    sampling_owner: call_policy.SamplingPolicyOwner,
    capture: ClientSet,
    emitted_events: EmittedEvents,
) -> None:
    inner = metering.make_recorder(lambda: "done", "probe.inner", sampling_owner, capture)
    outer = metering.make_recorder(inner, "probe.outer", sampling_owner, capture)
    with _execution_tally() as tally:
        assert outer() == "done"
    assert tally.snapshot() == {"probe.inner": 1, "probe.outer": 1}
    assert [fn for fn, _duration in _calls(emitted_events)] == ["probe.inner", "probe.outer"]


def test_recursive_public_entry_counts_each_invocation(
    sampling_owner: call_policy.SamplingPolicyOwner,
    capture: ClientSet,
    emitted_events: EmittedEvents,
) -> None:
    def body(depth: int) -> int:
        return recursive(depth - 1) + 1 if depth else 0

    recursive = metering.make_recorder(body, "probe.recursive", sampling_owner, capture)
    with _execution_tally() as tally:
        assert recursive(2) == 2
    assert tally.snapshot() == {"probe.recursive": 3}
    assert len(_calls(emitted_events)) == 3


def test_plugin_reload_and_multi_call_wrap_have_one_final_recorder(
    sampling_owner: call_policy.SamplingPolicyOwner,
    capture: ClientSet,
    emitted_events: EmittedEvents,
) -> None:
    bodies: list[str] = []

    def body() -> int:
        bodies.append("body")
        return 1

    def twice(inner: Callable[[], int]) -> int:
        return inner() + inner()

    registry = ExtensionRegistry(
        (
            (
                "probe",
                PluginContributions(
                    sdk_members=(SdkMember("self", "multi_probe_test", body),),
                    sdk_wraps=(
                        SdkWrap("self.multi_probe_test", twice),
                        SdkWrap("self.multi_probe_test", twice),
                    ),
                ),
            ),
        )
    )
    for _ in range(2):
        install.install(
            registry,
            config_images=DiskPluginConfigImages(resolve_meiva_home()),
            sampling=sampling_owner,
        )
        with _execution_tally() as tally:
            assert cast(Callable[[], int], meiva.self.multi_probe_test)() == 4
        assert tally.snapshot() == {"self.multi_probe_test": 1}
        install.uninstall()
    assert bodies == ["body"] * 8
    assert [fn for fn, _duration in _calls(emitted_events)] == ["self.multi_probe_test"] * 2
