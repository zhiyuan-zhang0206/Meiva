"""Unit tests for base/agents/sdk/telemetry.py — the SDK-usage runtime primitives.

Covers independent public entries, explicit execution tallies, call-local snapshots
and the event write (`emit`).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from base.agents.sdk import call_policy, keep_sample
from base.agents.sdk import telemetry as sdk_usage_telemetry
from base.agents.sdk.call_policy import SamplingPolicy
from base.agents.sdk.tally import SdkCallTally
from base.agents.sdk.tests.sampling_owners import (
    fixed_sampling_owner,
    gateway_sampling_owner,
    sampling_owner_failing_to_read,
    sampling_owner_serving,
)
from base.clock.tests.seeded_rng import seeded_rng
from base.host.net.tests.local_http import serve_json
from base.telemetry.tests.capture.event_capture import EmittedEvents
from base.telemetry.tests.capture.event_capture import emitted_events as emitted_events


@pytest.fixture
def sampling_owner() -> call_policy.SamplingPolicyOwner:
    return fixed_sampling_owner()


def _emitted_calls(emitted_events: EmittedEvents) -> list[tuple[str, dict[str, Any]]]:
    return [
        (str(event.attributes["fn"]), dict(event.attributes.get("detail") or {}))
        for event in emitted_events
    ]


# ── scope gate ────────────────────────────────────────────────────────────────


def test_emit_without_execution_tally(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    assert (
        sdk_usage_telemetry.run_metered(
            "ns.fn",
            lambda: "ok",
            (),
            {},
            identity={},
            sampling_owner=sampling_owner,
        )
        == "ok"
    )
    assert _emitted_calls(emitted_events) == [("ns.fn", {})]


def test_call_owns_its_entry_identity_snapshot(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    """Mutation of the caller's mapping during the body cannot relabel the emitted event."""
    identity: dict[str, Any] = {"agent_id": 41, "source": "agent:41"}

    def body() -> str:
        identity.update(agent_id=42, source="agent:42")
        return "done"

    assert (
        sdk_usage_telemetry.run_metered(
            "ns.fn",
            body,
            (),
            {},
            identity=identity,
            sampling_owner=sampling_owner,
        )
        == "done"
    )
    emitted = next(iter(emitted_events))
    assert emitted.agent_id == 41
    assert emitted.source == "agent:41"


def test_emit_with_explicit_execution_tally(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    tally = SdkCallTally()
    assert (
        sdk_usage_telemetry.run_metered(
            "ns.fn",
            lambda: "ok",
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
        == "ok"
    )
    assert _emitted_calls(emitted_events) == [("ns.fn", {})]


# ── independent public entries ───────────────────────────────────────────────


def test_each_public_entry_emits(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    def inner() -> str:
        return sdk_usage_telemetry.run_metered(
            "ns.inner",
            lambda: "inner",
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )

    tally = SdkCallTally()
    out = sdk_usage_telemetry.run_metered(
        "ns.outer",
        inner,
        (),
        {},
        identity={},
        tally=tally,
        sampling_owner=sampling_owner,
    )
    assert out == "inner"
    assert _emitted_calls(emitted_events) == [("ns.inner", {}), ("ns.outer", {})]


def test_return_and_exception_pass_through(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    _ = emitted_events

    def add(a: int, b: int) -> int:
        return a + b

    tally = SdkCallTally()
    assert (
        sdk_usage_telemetry.run_metered(
            "ns.fn",
            add,
            (2, 3),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
        == 5
    )

    def boom() -> None:
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        sdk_usage_telemetry.run_metered(
            "ns.boom",
            boom,
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )


def test_failed_call_still_emits(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    def boom() -> None:
        raise ValueError("x")

    tally = SdkCallTally()
    with pytest.raises(ValueError, match="x"):
        sdk_usage_telemetry.run_metered(
            "ns.boom",
            boom,
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
    assert _emitted_calls(emitted_events) == [
        ("ns.boom", {})
    ]  # invocation counts even when the call raises


def test_emit_carries_top_level_duration(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    sdk_usage_telemetry.emit(
        "shell.run",
        {"k": 1},
        duration=0.42,
        identity={},
        sampling_owner=sampling_owner,
    )
    assert [event.attributes for event in emitted_events] == [
        {"fn": "shell.run", "duration": 0.42, "detail": {"k": 1}, "sample_rate": 1}
    ]


def test_default_records_every_call(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    for _ in range(10):
        sdk_usage_telemetry.emit("files.read", identity={}, sampling_owner=sampling_owner)
    assert len(emitted_events) == 10
    assert all(event.attributes["sample_rate"] == 1 for event in emitted_events)


def test_emit_omits_detail_when_empty(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    sdk_usage_telemetry.emit("files.read", identity={}, sampling_owner=sampling_owner)
    assert "detail" not in next(iter(emitted_events)).attributes


def test_emit_does_not_classify_transport_errors_or_retry(
    sampling_owner: call_policy.SamplingPolicyOwner,
) -> None:
    failure = httpx.TimeoutException("not a transport owned by the SDK emitter")
    attempts: list[str] = []

    def fail() -> str:
        attempts.append("emit")
        raise failure

    with pytest.raises(httpx.TimeoutException) as raised:
        sdk_usage_telemetry.emit(
            "ns.fn", identity={}, sampling_owner=sampling_owner, machine_reader=fail
        )
    assert raised.value is failure
    assert attempts == ["emit"]


# ── explicit execution tally, independent of event sampling ───────────────────


def test_explicit_owner_collects_full_tally_per_fn(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    """Loop N executions count N; a call that never runs counts nothing; emit's
    spy sees the same order of calls."""
    tally = SdkCallTally()
    for _ in range(3):
        sdk_usage_telemetry.run_metered(
            "files.read",
            lambda: "ok",
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
    sdk_usage_telemetry.run_metered(
        "shell.run",
        lambda: "ok",
        (),
        {},
        identity={},
        tally=tally,
        sampling_owner=sampling_owner,
    )
    assert tally.snapshot() == {"files.read": 3, "shell.run": 1}
    assert [fn for fn, _ in _emitted_calls(emitted_events)] == [
        "files.read",
        "files.read",
        "files.read",
        "shell.run",
    ]


def test_live_sampling_keeps_tally_complete(
    emitted_events: EmittedEvents,
    tmp_path: Path,
) -> None:
    sampling_owner = sampling_owner_serving(
        SamplingPolicy(sampling_enabled=True, sample_every=10), tmp_path / "sampled.env"
    )
    tally = SdkCallTally()
    reference = seeded_rng()
    sampled = sum(keep_sample(10, reference.random()) for _ in range(40))
    assert sampled > 0
    for _ in range(40):
        sdk_usage_telemetry.run_metered(
            "files.read",
            lambda: None,
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
    sdk_usage_telemetry.run_metered(
        "files.read",
        lambda: None,
        (),
        {},
        identity={},
        tally=tally,
        sampling_owner=sampling_owner_serving(
            SamplingPolicy(sampling_enabled=False, sample_every=10), tmp_path / "unsampled.env"
        ),
    )
    assert [event.attributes["sample_rate"] for event in emitted_events] == [10] * sampled + [1]
    assert tally.snapshot() == {"files.read": 41}


def _invalid_policy_owner(
    failure: str, directory: Path, gateway_url: str
) -> call_policy.SamplingPolicyOwner:
    env_path = directory / ".env"
    if failure == "auth":
        return gateway_sampling_owner(gateway_url, env_path)
    if failure == "schema":
        owner = sampling_owner_serving(SamplingPolicy(), env_path)
        env_path.write_text("MEIVA_SDK_CALL_SAMPLE_EVERY=0\n")
        return owner
    return sampling_owner_failing_to_read(env_path)


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("failure", ["auth", "schema", "unreadable"])
async def test_invalid_policy_blocks_sdk_side_effects(
    tmp_path: Path,
    async_call: bool,
    failure: str,
) -> None:
    with serve_json({}, status=401) as gateway:
        sampling_owner = _invalid_policy_owner(failure, tmp_path, gateway.url)
        sampling_owner.refresh()
    calls: list[str] = []

    def body() -> None:
        calls.append("side effect")

    async def async_body() -> None:
        body()

    error = {
        "auth": httpx.HTTPStatusError,
        "schema": ValidationError,
        "unreadable": UnicodeDecodeError,
    }[failure]
    tally = SdkCallTally()
    for _ in range(2):
        with pytest.raises(error):
            if async_call:
                await sdk_usage_telemetry.run_metered_async(
                    "files.write",
                    async_body,
                    (),
                    {},
                    identity={},
                    tally=tally,
                    sampling_owner=sampling_owner,
                )
            else:
                sdk_usage_telemetry.run_metered(
                    "files.write",
                    body,
                    (),
                    {},
                    identity={},
                    tally=tally,
                    sampling_owner=sampling_owner,
                )
    assert tally.snapshot() == {}
    assert calls == []


@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("original", [ValueError("SDK failed"), asyncio.CancelledError()])
async def test_started_call_uses_its_snapshot_and_preserves_its_exception(
    emitted_events: EmittedEvents,
    tmp_path: Path,
    async_call: bool,
    original: BaseException,
) -> None:
    env_path = tmp_path / ".env"
    sampling_owner = sampling_owner_serving(SamplingPolicy(), env_path, refresh_seconds=3600.0)
    nested: list[str] = []

    def body() -> None:
        env_path.write_text("MEIVA_SDK_CALL_SAMPLE_EVERY=0\n")
        sampling_owner.refresh()
        with pytest.raises(ValidationError):
            sdk_usage_telemetry.run_metered(
                "inner.call",
                lambda: nested.append("must not run"),
                (),
                {},
                identity={},
                sampling_owner=sampling_owner,
            )
        raise original

    async def async_body() -> None:
        body()

    with pytest.raises(type(original)) as caught:
        if async_call:
            await sdk_usage_telemetry.run_metered_async(
                "outer.call",
                async_body,
                (),
                {},
                identity={},
                sampling_owner=sampling_owner,
            )
        else:
            sdk_usage_telemetry.run_metered(
                "outer.call",
                body,
                (),
                {},
                identity={},
                sampling_owner=sampling_owner,
            )
    assert caught.value is original
    assert nested == []
    assert next(iter(emitted_events)).attributes["fn"] == "outer.call"
    with pytest.raises(ValidationError):
        sdk_usage_telemetry.run_metered(
            "next.call",
            lambda: nested.append("must not run"),
            (),
            {},
            identity={},
            sampling_owner=sampling_owner,
        )
    assert nested == []


def test_direct_emit_propagates_policy_errors_without_creating_events(
    emitted_events: EmittedEvents, tmp_path: Path
) -> None:
    sampling_owner = sampling_owner_failing_to_read(tmp_path / ".env")
    sampling_owner.refresh()
    with pytest.raises(UnicodeDecodeError):
        sdk_usage_telemetry.emit("files.write", identity={}, sampling_owner=sampling_owner)
    assert len(emitted_events) == 0


def test_tally_counts_a_failed_public_entry_too(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    """Like its event, a call that raises still counts — it really executed."""
    _ = emitted_events

    def boom() -> None:
        raise ValueError("x")

    tally = SdkCallTally()
    with pytest.raises(ValueError, match="x"):
        sdk_usage_telemetry.run_metered(
            "ns.boom",
            boom,
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )
    assert tally.snapshot() == {"ns.boom": 1}


def test_tally_counts_each_public_entry(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    """Internal public SDK fan-out is a separate entry in the execution tally."""
    _ = emitted_events

    def inner() -> str:
        return sdk_usage_telemetry.run_metered(
            "ns.inner",
            lambda: "inner",
            (),
            {},
            identity={},
            tally=tally,
            sampling_owner=sampling_owner,
        )

    tally = SdkCallTally()
    sdk_usage_telemetry.run_metered(
        "ns.outer",
        inner,
        (),
        {},
        identity={},
        tally=tally,
        sampling_owner=sampling_owner,
    )
    assert tally.snapshot() == {"ns.inner": 1, "ns.outer": 1}


def test_calls_keep_their_explicit_tally_owner(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    _ = emitted_events
    sdk_usage_telemetry.run_metered(
        "ns.fn", lambda: "ok", (), {}, identity={}, sampling_owner=sampling_owner
    )  # framework-internal: no tally
    first = SdkCallTally()
    sdk_usage_telemetry.run_metered(
        "a.fn",
        lambda: "ok",
        (),
        {},
        identity={},
        tally=first,
        sampling_owner=sampling_owner,
    )
    second = SdkCallTally()
    sdk_usage_telemetry.run_metered(
        "b.fn",
        lambda: "ok",
        (),
        {},
        identity={},
        tally=second,
        sampling_owner=sampling_owner,
    )
    assert first.snapshot() == {"a.fn": 1}
    assert second.snapshot() == {"b.fn": 1}


def test_tally_entries_sorts_by_descending_count_then_method() -> None:
    assert sdk_usage_telemetry.tally_entries({"b.x": 2, "a.x": 2, "c.x": 5}) == [
        {"method": "c.x", "count": 5},
        {"method": "a.x", "count": 2},
        {"method": "b.x", "count": 2},
    ]
    assert sdk_usage_telemetry.tally_entries({}) == []


# ── reading the metadata back off exec_output messages ────────────────────────


def _exec_output(tool_call_id: str, sdk_calls: list[dict[str, Any]] | None) -> Any:
    from langchain_core.messages import ToolMessage

    kwargs: dict[str, Any] = {"meiva_msg_type": "exec_output"}
    if sdk_calls is not None:
        kwargs["sdk_calls"] = sdk_calls
    return ToolMessage(content="out", tool_call_id=tool_call_id, additional_kwargs=kwargs)


def test_sdk_calls_by_tool_call_id_reads_exec_output_metadata() -> None:
    """Present-and-empty maps to `[]` (a real zero); a message without the field
    (pre-tally history) stays absent from the map; other messages are ignored."""
    from langchain_core.messages import HumanMessage, ToolMessage

    messages = [
        HumanMessage(content="hi"),
        _exec_output("tc-1", [{"method": "files.read", "count": 3}]),
        _exec_output("tc-2", []),
        _exec_output("tc-3", None),
        ToolMessage(content="x", tool_call_id="tc-4"),
    ]
    index = sdk_usage_telemetry.sdk_calls_by_tool_call_id(messages)
    assert index == {
        "tc-1": [sdk_usage_telemetry.SdkCall(method="files.read", count=3)],
        "tc-2": [],
    }


def test_sdk_calls_by_tool_call_id_respects_the_start_window() -> None:
    """Only messages[start:] is scanned — the rendered span."""
    messages = [
        _exec_output("tc-1", [{"method": "files.read", "count": 1}]),
        _exec_output("tc-2", [{"method": "shell.run", "count": 1}]),
    ]
    assert set(sdk_usage_telemetry.sdk_calls_by_tool_call_id(messages, start=1)) == {"tc-2"}


# meiva:note
# Invokes `_sdk_cold_child.py` with a fixed argument vector to verify that a cold capture
# import failure rejects the metered body before executing it.
@pytest.mark.parametrize("error_type", [ImportError, RuntimeError])
@pytest.mark.parametrize("async_call", [False, True])
def test_local_capture_import_failure_rejects_body_with_original_exception(
    tmp_path: Path,
    error_type: type[Exception],
    async_call: bool,
) -> None:
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_sdk_cold_child.py")),
            "capture_import_failure",
        ],
        cwd=tmp_path,
        env={
            **{key: value for key, value in os.environ.items() if key != "MEIVA_CONFIG_BOOT"},
            "MEIVA_HOME": str(tmp_path / "absent-home"),
            "MEIVA_CONFIG_FETCH": "skip",
            "TEST_CAPTURE_ERROR": error_type.__name__,
            "TEST_CAPTURE_ASYNC": "1" if async_call else "0",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_no_local_participant_uses_the_real_optional_gate(
    emitted_events: EmittedEvents, sampling_owner: call_policy.SamplingPolicyOwner
) -> None:
    from base.agents.impersonation import manifest

    with manifest.admitted_local_sdk_call() as admission:
        assert admission is None

    def body() -> str:
        return "without-participant"

    assert (
        sdk_usage_telemetry.run_metered(
            "capture.call",
            body,
            (),
            {},
            identity={},
            sampling_owner=sampling_owner,
        )
        == "without-participant"
    )
    assert _emitted_calls(emitted_events) == [("capture.call", {})]


@pytest.mark.parametrize("error_type", ["ImportError", "RuntimeError"])
def test_cold_capture_import_failure_rejects_body(tmp_path: Path, error_type: str) -> None:
    result = subprocess.run(  # noqa: S603 — fixed argv, this interpreter runs the child script
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_sdk_cold_child.py")),
            "capture_import_failure",
        ],
        cwd=tmp_path,
        env={
            **{key: value for key, value in os.environ.items() if key != "MEIVA_CONFIG_BOOT"},
            "MEIVA_HOME": str(tmp_path / "absent-home"),
            "MEIVA_CONFIG_FETCH": "skip",
            "TEST_CAPTURE_ERROR": error_type,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
