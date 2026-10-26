"""SDK-call events and optional per-execution tallies.

Every wrapped public SDK entry emits by default, including nested, external Python
and framework calls. Live sampling policy affects events only. The execution owner
passes its optional full tally explicitly; it never gates instrumentation.
Each call retains the explicit caller-identity snapshot its recorder supplied.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Awaitable, Callable, Generator, Mapping
from typing import Any

from base.agents.sdk import SamplingPolicy, SamplingPolicyLike
from base.agents.sdk.capture import SdkCaptureOwner
from base.agents.sdk.tally import SdkCall, SdkCallTally, sdk_calls_by_tool_call_id
from base.api_contracts.agent_contract import SdkCaptureAdmission

# Event name written to events for each public SDK entry.
SDK_CALL_EVENT = "sdk_call"


def emit(
    fn: str,
    detail: Mapping[str, Any] | None = None,
    duration: float | None = None,
    *,
    identity: Mapping[str, Any],
    sampling_owner: SamplingPolicyLike,
    sampling_policy: SamplingPolicy | None = None,
    admission: SdkCaptureAdmission | None = None,
    producer: Callable[[], Any] | None = None,
    machine_reader: Callable[[], str] | None = None,
) -> None:
    """Write one ``sdk_call`` event; invalid input and emitter errors propagate.
    ``detail`` is omitted from the payload when empty, so a plain call stays ``{fn}``;
    ``duration`` (seconds, measured by ``run_metered``) rides as a top-level payload
    key — the registry declares it (``contract.SdkCall``), so a reader may reference
    ``attributes->>'duration'``. Policy errors propagate before emission; metered
    calls supply their entry snapshot so a refresh cannot mask the SDK outcome."""
    current = sampling_owner.read() if sampling_policy is None else sampling_policy
    every = current.sample_every if current.sampling_enabled else 1
    if not sampling_owner.should_sample(every):
        return
    extra: dict[str, Any] = {"fn": fn, "sample_rate": every}
    if detail:
        extra["detail"] = dict(detail)
    if duration is not None:
        extra["duration"] = duration
    from base import telemetry

    capture_args: dict[str, Any] = {} if admission is None else {"capture": admission.capture}
    if producer is not None:
        capture_args["producer"] = producer
    if machine_reader is not None:
        capture_args["machine_reader"] = machine_reader
    telemetry.emit(
        "telemetry",
        SDK_CALL_EVENT,
        attributes=extra,
        **identity,
        **capture_args,
    )


@contextlib.contextmanager
def _event_capture_admission(
    owner: SdkCaptureOwner | None,
) -> Generator[SdkCaptureAdmission | None, None, None]:
    """Use the optional gate; invalid capture code rejects the SDK call before its body."""
    from base.agents.impersonation.manifest import admitted_local_sdk_call

    with admitted_local_sdk_call(None if owner is None else owner.admit_sdk_call) as admission:
        yield admission


@contextlib.contextmanager
def _measure(
    fn: str,
    identity: Mapping[str, Any],
    tally: SdkCallTally | None,
    capture_owner: SdkCaptureOwner | None,
    sampling_owner: SamplingPolicyLike,
    producer: Callable[[], Any] | None,
    machine_reader: Callable[[], str] | None,
) -> Generator[None, None, None]:
    snapshot = sampling_owner.read()
    caller_identity = dict(identity)
    # Retain this call's original gate until its event is captured. Attachment
    # close can reject new entries but cannot seal this receipt before drain.
    with _event_capture_admission(capture_owner) as admission:
        t0 = time.monotonic()
        primary: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if tally is not None:
                tally.add(fn)
            try:
                capture_args: dict[str, Any] = {} if admission is None else {"admission": admission}
                emit(
                    fn,
                    duration=time.monotonic() - t0,
                    identity=caller_identity,
                    sampling_owner=sampling_owner,
                    sampling_policy=snapshot,
                    producer=producer,
                    machine_reader=machine_reader,
                    **capture_args,
                )
            except BaseException as secondary:
                if admission is not None:
                    try:
                        admission.capture_failed()
                    except BaseException as capture_error:
                        secondary.add_note(
                            f"Receipt failure recording also failed: {capture_error!r}"
                        )
                if primary is None:
                    raise
                primary.add_note(f"SDK call {fn!r} event emission also failed: {secondary!r}")
                for note in getattr(secondary, "__notes__", ()):
                    primary.add_note(note)


def run_metered[ResultT](
    fn: str,
    original: Callable[..., ResultT],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    sampling_owner: SamplingPolicyLike,
    tally: SdkCallTally | None = None,
    capture_owner: SdkCaptureOwner | None = None,
    producer: Callable[[], Any] | None = None,
    machine_reader: Callable[[], str] | None = None,
) -> ResultT:
    """Validate each public entry before execution and retain its call-local snapshots."""
    with _measure(fn, identity, tally, capture_owner, sampling_owner, producer, machine_reader):
        return original(*args, **kwargs)


async def run_metered_async[ResultT](
    fn: str,
    original: Callable[..., Awaitable[ResultT]],
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    *,
    identity: Mapping[str, Any],
    sampling_owner: SamplingPolicyLike,
    tally: SdkCallTally | None = None,
    capture_owner: SdkCaptureOwner | None = None,
    producer: Callable[[], Any] | None = None,
    machine_reader: Callable[[], str] | None = None,
) -> ResultT:
    """Validate when awaited, preserving cancellation and the call's own admission."""
    with _measure(fn, identity, tally, capture_owner, sampling_owner, producer, machine_reader):
        return await original(*args, **kwargs)


# ── the wire-side counts: entry model + materialization + read-back ───────────


def tally_entries(tally: Mapping[str, int]) -> list[dict[str, Any]]:
    """Materialize a recording tally as the wire list that rides the exec result:
    ``[{"method": "<ns>.<fn>", "count": N}, ...]``, sorted by descending count then
    method — the collapsed-code chip's render order (JSON dicts, one per fn)."""
    entries: list[dict[str, Any]] = []
    for fn, count in sorted(tally.items(), key=lambda item: (-item[1], item[0])):
        entries.append({"method": fn, "count": count})
    return entries


__all__ = [
    "SDK_CALL_EVENT",
    "SdkCall",
    "emit",
    "run_metered",
    "run_metered_async",
    "sdk_calls_by_tool_call_id",
    "tally_entries",
]
