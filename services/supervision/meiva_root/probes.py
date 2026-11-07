"""One readiness contract per selected root unit.

Service probes come from the actual manifest roster and must exist. Static
host diagnostics may register a lazy reference. Probes report observations;
only the root supervisor owns service mutations.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import math
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Event, Thread
from typing import Protocol, TypeGuard

from base.daemon.health import DaemonProbe
from base.host.boundary.untyped import object_of

_log = logging.getLogger(__name__)

Probe = Callable[[], DaemonProbe]
"""A total probe: returns a verdict, never raises (the healthcheck contract)."""


def _is_probe(value: object) -> TypeGuard[Probe]:
    return callable(value)


class ProbeSource(Protocol):
    """The slice of a service spec the registry reads.

    Duck-typed rather than imported: the registry only needs these three facts,
    and the spec type lives a layer away. Declared read-only so immutable
    providers (frozen dataclass specs) satisfy it as well as plain attributes.
    """

    @property
    def session(self) -> str: ...

    @property
    def healthcheck_module(self) -> str | None: ...

    @property
    def identity_probe(self) -> Probe | None: ...


class ProbeError(ValueError):
    """A probe registration or reference is malformed or cannot be resolved."""


@dataclass(slots=True)
class _Entry:
    """One registration: a probe callable, or a lazy reference to one."""

    probe: Probe | None = None
    ref: str | None = None


def _split_ref(ref: str) -> tuple[str, str]:
    """`"module:attribute"` into its parts; anything else is a wiring bug."""
    module_name, sep, attribute = ref.partition(":")
    if not sep or not module_name or not attribute or ":" in attribute:
        raise ProbeError(f"probe reference {ref!r} is not 'module:attribute'")
    return module_name, attribute


def resolve_ref(ref: str) -> Probe:
    """Import the module a reference names and return its attribute.

    Resolution failures surface as `ProbeError`; the runner treats an
    unresolvable probe as "no verdict available" and never restarts a unit it
    cannot verify (fail-closed).
    """
    module_name, attribute = _split_ref(ref)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ProbeError(f"cannot import probe module {module_name!r}: {exc}") from exc
    try:
        resolved = object_of(getattr, module, attribute)
    except AttributeError as exc:
        raise ProbeError(f"module {module_name!r} has no attribute {attribute!r}") from exc
    if not _is_probe(resolved):
        raise ProbeError(f"probe reference {ref!r} resolves to a non-callable")
    return resolved


class ProbeRegistry:
    """`unit id -> probe`, resolved from code (specs or static registrations).

    Registration is fail-fast: a duplicate unit id is rejected rather than
    silently overwritten — two probes for one unit is a wiring bug, and keeping
    the last one written would hide it.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def register(self, unit_id: str, probe: Probe) -> None:
        """Register `probe` for `unit_id` (the static path, direct callable)."""
        self._reject_duplicate(unit_id)
        self._entries[unit_id] = _Entry(probe=probe)

    def register_ref(self, unit_id: str, ref: str) -> None:
        """Register `unit_id` against `"module:attribute"`, resolved lazily.

        The reference is validated eagerly (fail-fast) but the module is only
        imported when the unit is first probed, so registering the
        host-policy / data-plane class costs no imports until the wiring slice
        actually runs those probes.
        """
        self._reject_duplicate(unit_id)
        _split_ref(ref)
        self._entries[unit_id] = _Entry(ref=ref)

    def register_specs(self, specs: Iterable[ProbeSource]) -> None:
        """Register every selected service; missing readiness is a wiring error."""
        for spec in specs:
            if spec.identity_probe is None:
                raise ProbeError(f"root unit {spec.session!r} has no readiness probe")
            self.register(spec.session, spec.identity_probe)

    def unit_ids(self) -> tuple[str, ...]:
        """Registered unit ids, in registration order (the round's order)."""
        return tuple(self._entries)

    def resolve(self, unit_id: str) -> Probe:
        """The probe for `unit_id`; resolves and caches a lazy reference."""
        try:
            entry = self._entries[unit_id]
        except KeyError:
            raise ProbeError(f"no probe registered for unit {unit_id!r}") from None
        if entry.probe is None:
            ref = entry.ref
            if ref is None:
                raise ProbeError(f"probe entry for unit {unit_id!r} carries neither probe nor ref")
            entry.probe = resolve_ref(ref)
        return entry.probe

    def _reject_duplicate(self, unit_id: str) -> None:
        if not unit_id:
            raise ProbeError("unit id must be a non-empty string")
        if unit_id in self._entries:
            raise ProbeError(f"a probe for unit {unit_id!r} is already registered")


# meiva:note
# Own one native observation through its real completion or bounded join: the probe runs on a
# daemon thread (Python cannot cancel a blocked native call), and `future` carries its verdict
# to the round that awaits it.
class _ProbeObservation:
    def __init__(self, probe: Probe) -> None:
        self.future: Future[DaemonProbe] = Future()
        self._probe = probe
        self._stop = Event()
        self._error: BaseException | None = None
        self._thread = Thread(target=self._run, name="root-probe", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            if self._stop.is_set():
                self.future.set_result(DaemonProbe.unavailable("observation admission closed"))
            else:
                self.future.set_result(self._sample())
        except BaseException as exc:
            self._error = exc
            _log.error("root observation worker failed", exc_info=exc)
            self.future.set_exception(exc)

    # meiva:note
    # Inspection failures remain unknown evidence, never recovery evidence.
    def _sample(self) -> DaemonProbe:
        try:
            result = self._probe()
        except Exception as exc:
            return DaemonProbe.unavailable(f"probe raised {type(exc).__name__}: {exc}")
        if not isinstance(result, DaemonProbe):
            return DaemonProbe.unavailable("probe did not return DaemonProbe")
        return result

    def close(self) -> None:
        self._stop.set()

    # meiva:note
    # Stop admission and observe the real worker; its native call may remain live and is then
    # reported unfinished rather than waited for.
    def join(self, timeout_s: float) -> bool:
        self.close()
        self._thread.join(timeout=timeout_s)
        complete = not self._thread.is_alive()
        if self._error is not None:
            raise self._error
        return complete


# meiva:note
# One bounded synchronous observation per probe, with at most one daemon worker.
#
# A deadline reports UNAVAILABLE and retains the worker until it exits; later rounds cannot
# replace it, and its late result is discarded before a fresh observation starts. Daemon
# threads cannot hold root shutdown hostage.
class ProbeRunner:
    def __init__(self) -> None:
        self._observation: _ProbeObservation | None = None
        self._closed = False

    # meiva:note
    # Prevent fresh observations before any worker join begins.
    def close(self) -> None:
        self._closed = True
        if self._observation is not None:
            self._observation.close()

    # meiva:note
    # Bounded best effort; a late worker error remains owned and is raised on a repeated stop.
    def stop(self, timeout_s: float = 0.2) -> bool:
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise ValueError("probe join timeout must be finite and nonnegative")
        self.close()
        return self._observation is None or self._observation.join(timeout_s)

    # meiva:note
    # A timed-out or cancelled caller never awaits this wrapper again, so the exception it
    # carries is consumed here; the native observation retains and raises the same original
    # error at stop.
    @staticmethod
    def _retrieve(future: asyncio.Future[DaemonProbe]) -> None:
        if not future.cancelled():
            future.exception()

    # meiva:note
    # One verdict; an observation still running at its deadline is UNAVAILABLE.
    async def observe(self, probe: Probe, timeout_s: float) -> DaemonProbe:
        if self._closed:
            raise RuntimeError("probe runner admission closed")
        if self._observation is not None and not self._observation.future.done():
            return DaemonProbe.unavailable("previous observation still running after its deadline")
        if self._observation is not None and not self._observation.join(0):
            return DaemonProbe.unavailable("previous observation worker still completing")
        observation = _ProbeObservation(probe)
        self._observation = observation
        pending = asyncio.wrap_future(observation.future)
        pending.add_done_callback(self._retrieve)
        try:
            result = await asyncio.wait_for(asyncio.shield(pending), timeout_s)
        except TimeoutError:
            return DaemonProbe.unavailable(f"observation exceeded {timeout_s:g}s deadline")
        return result
