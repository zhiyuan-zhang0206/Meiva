"""Per-call SDK usage metering — the wrapping half of the SDK Usage instrumentation
(the call-local admission and emit path live in ``base/agents/sdk/telemetry.py``).

Every public ``meiva.*`` callable is wrapped, once, by a transparent recorder installed by
the SDK installation (``meiva.sdk_surface.install``) over the final surface — after the plugin
load, outermost of any plugin wrap layer. On each public entry the recorder (via ``run_metered``) writes one ``sdk_call``
event into the unified ``events`` stream, carrying the dotted function name in ``attributes.fn``
(``files.read``, ``shell.run``, ``self.compact``). the Grafana call-frequency ranking sums those events — replacing the old regex
scrape of code-event *source text*, which counted any ``meiva.X(`` occurrence in comments,
string literals, docstrings, and agent-written example code (so ``meiva._private(`` from a
private call, ``meiva.bootDefaultActor(`` from a comment, and ``meiva.x.y(`` from a
placeholder string all showed up as "SDK usage").

Transparency contract — the recorder MUST NOT perturb the SDK surface:
  - ``functools.wraps(original)`` copies name / qualname / module / doc / ``__dict__``
    and sets ``__wrapped__``, so ``inspect.signature`` (and therefore ``meiva.help``)
    resolves the original signature byte-for-byte, and function-attached members
    (``meiva.understand.UnderstandError``) survive via the ``__dict__`` copy.
  - A valid sampling policy is captured before each call executes. Invalid
    configuration or caller identity prevents execution; transient fetch failures
    may use its last valid snapshot. An emitter error after a successful body
    propagates without retrying the body.
    If the body already failed, its original exception and cause remain primary;
    the emitter failure is attached as an exception note, including cancellation
    and lifecycle exceptions.

Every public call is metered, including bare Python, CLI and external attachments.
Nested public entries count independently. The process-local execution context
holds an optional tally; recorders pass that owner explicitly to each call.
Static functions are wrapped by the installation; dynamic
MCP calls are wrapped at their common call funnel.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType, SimpleNamespace
from typing import Any, cast

from base.agents.sdk.call_policy import SamplingPolicyOwner
from base.agents.sdk.capture import SdkCaptureOwner
from base.agents.sdk.tally import SdkCallTally
from meiva.sdk_surface import process_context
from meiva.sdk_surface.discovery import agent_visible_names
from meiva.sdk_surface.installation import EventPipelines
from meiva.sdk_surface.pure.caller.identity_plan import caller_facts
from meiva.sdk_surface.pure.caller.usage_plan import usage_identity

# A recorder marks itself with a reference to itself. `is_recorder` tests that identity, so
# `install()` skips a target only when the current top callable *is* a recorder — robust to
# `meiva.extend`'s wrap machinery, which copies a wrapped callable's __dict__: a plugin wrapper built
# over a recorder inherits the marker, but it points at the inner recorder, not at the wrapper.
_RECORDER_MARK = "__meiva_recorder__"
_SAMPLING_MARK = "__meiva_sampling_owner__"


def _recorder(fn: Callable[..., Any], sampling: SamplingPolicyOwner) -> Callable[..., Any]:
    setattr(fn, _RECORDER_MARK, fn)
    setattr(fn, _SAMPLING_MARK, sampling)
    return fn


def is_recorder(fn: object) -> bool:
    """Whether `fn` is itself a metering recorder (not a wrapper that merely copied one's attributes)."""
    return fn is not None and getattr(fn, _RECORDER_MARK, None) is fn


def _check_sampling(fn: object, sampling: SamplingPolicyOwner) -> None:
    if is_recorder(fn) and getattr(fn, _SAMPLING_MARK) is not sampling:
        raise ValueError("SDK recorders already belong to another sampling owner")


def _caller(
    clients: EventPipelines | None,
) -> tuple[
    dict[str, Any],
    SdkCallTally | None,
    SdkCaptureOwner | None,
    Callable[[], Any] | None,
    Callable[[], str] | None,
]:
    """Snapshot this call's provenance; invalid identity rejects admission."""
    from base.agents.messages.external_caller import external_caller

    bound = process_context.ambient_context()
    own = None if bound is None else bound.identity
    borrowed = own.lease.agent_id if own is not None and own.lease is not None else None
    external = external_caller()
    identity = usage_identity(
        borrowed, caller_facts(own), None if external is None else external.source()
    )
    if bound is None:
        return identity, None, None, None if clients is None else clients.event_pipeline, None
    return (
        identity,
        bound.sdk_calls,
        bound.sdk_capture,
        bound.clients.event_pipeline,
        None if bound.machine is None else bound.machine.name,
    )


def make_recorder[F: Callable[..., object]](
    original: F,
    fq: str,
    sampling: SamplingPolicyOwner,
    clients: EventPipelines | None,
) -> F:
    """Transparent proxy around ``original`` that meters the call as ``fq`` (the call /
    tally / emit logic lives in ``base.agents.sdk.telemetry.run_metered``)."""

    @functools.wraps(original)
    def recorder(*args: Any, **kwargs: Any) -> Any:
        from base.agents.sdk.telemetry import run_metered

        identity, tally, capture_owner, call_producer, machine_reader = _caller(clients)
        return run_metered(
            fq,
            original,
            args,
            kwargs,
            identity=identity,
            tally=tally,
            capture_owner=capture_owner,
            sampling_owner=sampling,
            producer=call_producer,
            machine_reader=machine_reader,
        )

    if inspect.iscoroutinefunction(original):

        @functools.wraps(original)
        async def async_recorder(*args: Any, **kwargs: Any) -> Any:
            from base.agents.sdk import telemetry as sdk_usage_telemetry

            identity, tally, capture_owner, call_producer, machine_reader = _caller(clients)
            return await sdk_usage_telemetry.run_metered_async(
                fq,
                original,
                args,
                kwargs,
                identity=identity,
                tally=tally,
                capture_owner=capture_owner,
                sampling_owner=sampling,
                producer=call_producer,
                machine_reader=machine_reader,
            )

        return cast("F", _recorder(async_recorder, sampling))

    return cast("F", _recorder(recorder, sampling))


# meiva.mcps exposes its tools dynamically (the servers come from configuration), so the namespace
# walk cannot reach them. Every MCP tool call - text form `meiva.mcps.<server>.<tool>(...)` and the
# `raw` form - funnels through `meiva.mcps.call_funnel.call_raw(source, server, tool, ...)`, so one
# wrapper there meters them all, keyed by the runtime server + tool. `meiva.mcps.call_funnel` is
# the module that holds it.
_MCP_CALL_FUNNEL = "call_raw"


def make_mcp_recorder[F: Callable[..., object]](
    original: F,
    sampling: SamplingPolicyOwner,
    clients: EventPipelines | None,
) -> F:
    """Recorder for the MCP call funnel — derives the fq from the runtime args."""

    @functools.wraps(original)
    def recorder(source: Any, server: str, tool: str, *args: Any, **kwargs: Any) -> Any:
        from base.agents.sdk.telemetry import run_metered

        identity, tally, capture_owner, call_producer, machine_reader = _caller(clients)
        return run_metered(
            f"mcps.{server}.{tool}",
            original,
            (source, server, tool, *args),
            kwargs,
            identity=identity,
            tally=tally,
            capture_owner=capture_owner,
            sampling_owner=sampling,
            producer=call_producer,
            machine_reader=machine_reader,
        )

    return cast("F", _recorder(recorder, sampling))


# meiva:note
# Walk the namespace tree under `root` → ``(parent, attr, fq)`` for every public callable.
#
# ``fq`` is the dotted path under ``meiva`` (``files.read``, ``shell.sessions.new``,
# ``understand``) — the key space the metric reports. Recurses into sub-namespaces (modules /
# SimpleNamespace); targets routines; skips classes, constants, and dynamic namespaces served by
# ``__getattr__``.
def _instrument_targets(root: object) -> list[tuple[object, str, str]]:
    targets: list[tuple[object, str, str]] = []
    seen: set[int] = set()

    def walk(container: Any, prefix: str) -> None:
        if id(container) in seen:
            return
        seen.add(id(container))
        # meiva:note
        # agent_visible_names is the single source of truth for the agent surface (help / SDK-
        # expand / doc-lint share it). It reads __all_for_meiva__ statically so a dynamic proxy
        # surface is never force-evaluated, and raises for a container that declares none.
        for name in agent_visible_names(container):
            # meiva:note
            # getattr_static, never getattr: a dynamically-served member (e.g.
            # meiva.self.MACHINE_SPEC, computed via module __getattr__) must not be force-
            # evaluated — it can raise (MachineNameMissing when unset) and crash install() ->
            # load_extensions. Statically-resolvable functions and submodules (the only things
            # we wrap / recurse) are all real attributes.
            attr = inspect.getattr_static(container, name, None)
            if attr is None:
                continue
            fq = f"{prefix}{name}"
            if inspect.isroutine(attr):
                targets.append((container, name, fq))
            elif isinstance(attr, (ModuleType, SimpleNamespace)):
                walk(attr, f"{fq}.")

    walk(root, "")
    return targets


# meiva:note
# What an installation meters: every public routine under the `meiva` module (the walk) and the
# module that holds the MCP call funnel, whose dynamic tool calls the walk cannot reach. Read once
# from the module the installer writes; `install` takes the value, so a test meters a namespace
# it builds.
@dataclass(frozen=True)
class MeterSurface:
    routines: tuple[tuple[object, str, str], ...]
    funnel_module: object | None


def read_surface(root: ModuleType) -> MeterSurface:
    funnel_module = getattr(getattr(root, "mcps", None), "call_funnel", None)
    return MeterSurface(routines=tuple(_instrument_targets(root)), funnel_module=funnel_module)


# meiva:note
# Wrap every public ``meiva.*`` callable of `surface` with the recording proxy. Idempotent.
#
# Called by ``meiva.sdk_surface.install`` after plugins load, so plugin namespaces / members /
# declared wrap layers are all present and get metered too (the recorder sits outermost of any
# plugin wrap). Re-running only wraps targets whose current top callable is not already a
# recorder, so it is safe to call on every plugin reload — a newly plugin-wrapped target gets a
# fresh outermost recorder.
#
# Returns the restore ledger: the ``(parent, attr)`` pairs this call actually wrapped, in wrap
# order. The installation carries it; ``uninstall(ledger)`` restores from it.
def install(
    sampling: SamplingPolicyOwner, *, clients: EventPipelines | None, surface: MeterSurface
) -> tuple[tuple[object, str], ...]:
    funnel_mod = surface.funnel_module
    funnel = None if funnel_mod is None else getattr(funnel_mod, _MCP_CALL_FUNNEL, None)
    for parent, attr, _fq in surface.routines:
        _check_sampling(getattr(parent, attr, None), sampling)
    _check_sampling(funnel, sampling)
    wrapped: list[tuple[object, str]] = []
    for parent, attr, fq in surface.routines:
        current = getattr(parent, attr, None)
        if current is None or is_recorder(current):
            continue
        setattr(parent, attr, make_recorder(current, fq, sampling, clients))
        wrapped.append((parent, attr))

    if funnel_mod is not None and callable(funnel) and not is_recorder(funnel):
        setattr(funnel_mod, _MCP_CALL_FUNNEL, make_mcp_recorder(funnel, sampling, clients))
        wrapped.append((funnel_mod, _MCP_CALL_FUNNEL))
    return tuple(wrapped)


def uninstall(ledger: tuple[tuple[object, str], ...]) -> None:
    """Restore every metered target in `ledger` to the callable the recorder wraps — test
    teardown, so a test that installs the recorders does not leak them into the
    shared ``meiva`` singleton the rest of the suite imports.

    ``ledger`` is the record ``install()`` returned — the ``(parent, attr)`` pairs it
    actually wrapped, in wrap order; the Installation carries it
    (``installation.installed().metered``), and the consumer-local SDK restore
    (``meiva/tests/fixtures/sdk.py``) passes that ledger. Direct callers own their
    returned ledger and restore it in their own ``finally``.

    Restore from the record, never by re-walking the namespace: the walk resolves
    dynamic member surfaces (``meiva.skills``'s index scans the skills tree and reads
    the install registry) and must not run at teardown, where a test's
    deliberately-broken state can make it raise — the failure shape of task #3426
    (macOS local: 8 teardowns exploded after the test bodies had gone green).
    """
    # O(1) early-out: the ledger is empty when nothing was wrapped. Without it every
    # per-test teardown would pay for a full restore pass that could only find nothing.
    if not ledger:
        return
    for parent, attr in ledger:
        current = getattr(parent, attr, None)
        if current is not None and is_recorder(current):
            setattr(parent, attr, current.__wrapped__)
