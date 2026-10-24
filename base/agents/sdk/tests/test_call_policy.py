"""Live sampling uses validated snapshots and never blocks SDK calls on a fetch."""

import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from base.agents.sdk import call_policy
from base.agents.sdk.call_policy import SamplingPolicy, SamplingSource
from base.agents.sdk.tests.sampling_owners import (
    fixed_sampling_owner,
    gateway_sampling_owner,
    sampling_owner_failing_to_read,
    sampling_owner_serving,
)
from base.host.net.tests.local_http import closed_port_url, serve_hangup, serve_json

_ENABLED_EVERY_3 = {"MEIVA_SDK_CALL_SAMPLING_ENABLED": "true", "MEIVA_SDK_CALL_SAMPLE_EVERY": "3"}


@pytest.mark.parametrize("serves_gateway", [True, False])
def test_local_policy_reads_live_file(tmp_path: Path, serves_gateway: bool) -> None:
    env_file = tmp_path / ".env"
    env = {"MEIVA_MACHINE_SERVE_GATEWAY": "true"} if serves_gateway else {}
    source = SamplingSource(env=env, env_path=env_file)

    env_file.write_text("MEIVA_SDK_CALL_SAMPLING_ENABLED=true\nMEIVA_SDK_CALL_SAMPLE_EVERY=4\n")
    assert call_policy.read_policy(source) == SamplingPolicy(sampling_enabled=True, sample_every=4)
    env_file.write_text("MEIVA_SDK_CALL_SAMPLING_ENABLED=false\nMEIVA_SDK_CALL_SAMPLE_EVERY=7\n")
    assert call_policy.read_policy(source) == SamplingPolicy(sampling_enabled=False, sample_every=7)
    env_file.write_text("")
    assert call_policy.read_policy(source) == SamplingPolicy()


def test_the_launch_environment_alone_gives_the_first_snapshot() -> None:
    assert call_policy.policy_from_aliases({}) == SamplingPolicy()
    assert call_policy.policy_from_aliases(_ENABLED_EVERY_3) == SamplingPolicy(
        sampling_enabled=True, sample_every=3
    )


def test_a_negative_refresh_wait_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        SamplingSource(env={}, env_path=tmp_path / ".env", refresh_seconds=-1.0)


def test_remote_policy_refreshes_without_waiting_for_network(tmp_path: Path) -> None:
    gate = threading.Event()
    with serve_json(_ENABLED_EVERY_3, gate=gate) as gateway:
        cache = gateway_sampling_owner(gateway.url, tmp_path / ".env")
        try:
            assert cache.read() == SamplingPolicy()
            assert gateway.arrived.wait(2)
            assert cache.read() == SamplingPolicy()
            assert len(gateway.requests) == 1
        finally:
            gate.set()
        worker = cache.worker
        assert worker is not None and worker.completed.wait(5)
        assert cache.read() == SamplingPolicy(sampling_enabled=True, sample_every=3)


def test_the_first_refresh_is_due_one_wait_after_the_owner_is_built(tmp_path: Path) -> None:
    owner = sampling_owner_serving(SamplingPolicy(), tmp_path / ".env", refresh_seconds=3600.0)
    for _ in range(3):
        assert owner.read() == SamplingPolicy()
    assert owner.worker is None


def test_failed_refresh_preserves_policy_and_warns(tmp_path: Path) -> None:
    cache = gateway_sampling_owner(
        closed_port_url(),
        tmp_path / ".env",
        initial=SamplingPolicy(sampling_enabled=True, sample_every=5),
    )
    reports: list[Any] = []
    from loguru import logger

    sink = logger.add(lambda message: reports.append(message.record))
    try:
        cache.refresh()
    finally:
        logger.remove(sink)
    assert cache.value.sample_every == 5
    # meiva:note
    # The sink is process-wide: keep this module's own report, not whichever
    # warning another producer emitted first.
    warned = [
        row
        for row in reports
        if row["level"].name == "WARNING" and "sampling config fetch unavailable" in row["message"]
    ]
    assert warned and warned[0]["exception"] is not None  # the traceback rides it (#4979)
    assert warned[0]["exception"].type is httpx.ConnectError
    assert warned[0]["extra"]["_no_emitter"] is True
    assert not cache.refreshing


@pytest.mark.parametrize("value", [0, -1, 1.5])
def test_ratio_rejects_invalid_values(value: Any) -> None:
    with pytest.raises(ValidationError):
        SamplingPolicy(sample_every=value)


def test_enrolled_process_reads_gateway_policy(tmp_path: Path) -> None:
    with serve_json(_ENABLED_EVERY_3) as gateway:
        env = {
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
            "MEIVA_GATEWAY_URL": gateway.url,
            "MEIVA_API_TOKEN": "machine-token",
        }
        policy = call_policy.read_policy(SamplingSource(env=env, env_path=tmp_path / ".env"))
    assert policy == SamplingPolicy(sampling_enabled=True, sample_every=3)
    assert [r.headers["authorization"] for r in gateway.requests] == ["Bearer machine-token"]
    assert [r.path for r in gateway.requests] == ["/api/bootstrap"]


@pytest.mark.parametrize("outage", ["refused", "timeout", "hangup", "rate_limited"])
def test_expected_fetch_failure_retains_the_valid_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, outage: str
) -> None:
    from base.telemetry import emitter

    def initialize(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("a sampling fetch failure must not initialize an event pipeline")

    monkeypatch.setattr(emitter, "init_telemetry", initialize)
    snapshot = SamplingPolicy(sampling_enabled=True, sample_every=5)
    env_path = tmp_path / ".env"
    if outage == "refused":
        cache = gateway_sampling_owner(closed_port_url(), env_path, initial=snapshot)
        cache.refresh()
    elif outage == "timeout":
        with serve_json({}, delay_s=1.0) as gateway:
            cache = gateway_sampling_owner(gateway.url, env_path, initial=snapshot, timeout_s=0.2)
            cache.refresh()
    elif outage == "hangup":
        with serve_hangup() as url:
            cache = gateway_sampling_owner(url, env_path, initial=snapshot)
            cache.refresh()
    else:
        with serve_json({}, status=429) as gateway:
            cache = gateway_sampling_owner(gateway.url, env_path, initial=snapshot)
            cache.refresh()
    assert cache.read() is snapshot


@pytest.mark.parametrize("failure", ["unauthorized", "server_error", "bad_url", "unreadable_file"])
def test_unexpected_refresh_error_persists_until_a_valid_snapshot(
    tmp_path: Path, failure: str
) -> None:
    env_path = tmp_path / ".env"
    if failure == "unreadable_file":
        cache = sampling_owner_failing_to_read(env_path, refresh_seconds=3600.0)
        cache.refresh()
    elif failure == "bad_url":
        cache = gateway_sampling_owner("ftp://gateway.invalid", env_path, refresh_seconds=3600.0)
        cache.refresh()
    else:
        status = 401 if failure == "unauthorized" else 500
        with serve_json({}, status=status) as gateway:
            cache = gateway_sampling_owner(gateway.url, env_path, refresh_seconds=3600.0)
            cache.refresh()
    assert cache.error is not None
    original = cache.error[0]
    depths: list[int] = []
    for _ in range(2):
        with pytest.raises(type(original)) as caught:
            cache.read()
        assert caught.value is original
        depths.append(sum(1 for _ in traceback.walk_tb(caught.value.__traceback__)))
    assert depths[0] == depths[1]  # Repeated callers do not accumulate each other's stacks.
    assert cache.worker is None


def test_an_outage_does_not_clear_an_invalid_configuration(tmp_path: Path) -> None:
    with serve_json({}, status=500) as gateway:
        cache = gateway_sampling_owner(gateway.url, tmp_path / ".env")
        cache.refresh()
    assert cache.error is not None
    original = cache.error[0]

    cache.refresh()

    assert cache.error is not None and cache.error[0] is original
    with pytest.raises(type(original)):
        cache.read()


def test_a_valid_snapshot_clears_an_invalid_configuration(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    cache = sampling_owner_failing_to_read(env_path, refresh_seconds=3600.0)
    cache.refresh()
    with pytest.raises(UnicodeDecodeError):
        cache.read()

    env_path.write_text("MEIVA_SDK_CALL_SAMPLING_ENABLED=true\nMEIVA_SDK_CALL_SAMPLE_EVERY=7\n")
    cache.refresh()

    assert cache.read() == SamplingPolicy(sampling_enabled=True, sample_every=7)


def test_invalid_schema_is_not_reported_as_a_retained_policy(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    cache = sampling_owner_serving(SamplingPolicy(), env_path)
    env_path.write_text("MEIVA_SDK_CALL_SAMPLE_EVERY=0\n")
    cache.refresh()
    with pytest.raises(ValidationError):
        cache.read()


def test_failed_policy_read_still_starts_a_refresh_when_due(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    cache = sampling_owner_serving(SamplingPolicy(), env_path, refresh_seconds=0.0)
    env_path.write_text("MEIVA_SDK_CALL_SAMPLE_EVERY=0\n")
    cache.refresh()
    snapshot = SamplingPolicy(sample_every=9)
    env_path.write_text("MEIVA_SDK_CALL_SAMPLE_EVERY=9\n")

    with pytest.raises(ValidationError):
        cache.read()
    worker = cache.worker
    assert worker is not None and worker.completed.wait(5)
    assert cache.read() == snapshot


def test_sdk_telemetry_cold_import_does_not_load_httpx(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 — fixed argv, this interpreter runs the child script
        [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("_sdk_cold_child.py")),
            "cold_import_loads_no_httpx",
        ],
        cwd=tmp_path,
        env={**os.environ, "MEIVA_HOME": str(tmp_path / "home"), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_refresh_dependency_import_failure_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = fixed_sampling_owner()
    monkeypatch.setitem(sys.modules, "httpx", None)
    cache.refresh()
    with pytest.raises(ImportError, match="httpx"):
        cache.read()


def test_invalid_refresh_dependency_classification_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = fixed_sampling_owner()
    monkeypatch.delattr(httpx, "HTTPStatusError")
    cache.refresh()
    with pytest.raises(AttributeError, match="HTTPStatusError"):
        cache.read()
