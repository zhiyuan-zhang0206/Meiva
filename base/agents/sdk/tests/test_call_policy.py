"""Live sampling uses validated snapshots and never blocks SDK calls on a fetch."""

import os
import subprocess
import sys
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from base.agents.sdk import call_policy
from base.agents.sdk.call_policy import SamplingPolicy
from base.host.net.tests.local_http import serve_json


# meiva:note
# A fetch that answers with one scripted outcome, replaceable between refreshes: a policy to
# return or an exception to raise.
class ScriptedFetch:
    def __init__(self, outcome: SamplingPolicy | BaseException) -> None:
        self.outcome = outcome

    def __call__(self) -> SamplingPolicy:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _owner(
    fetch: Callable[[], SamplingPolicy], initial: SamplingPolicy | None = None
) -> call_policy.SamplingPolicyOwner:
    return call_policy.SamplingPolicyOwner(
        initial=SamplingPolicy() if initial is None else initial, fetch=fetch
    )


@pytest.mark.parametrize("serves_gateway", [True, False])
def test_local_policy_reads_live_file(tmp_path: Path, serves_gateway: bool) -> None:
    env_file = tmp_path / ".env"
    env = {"MEIVA_MACHINE_SERVE_GATEWAY": "true"} if serves_gateway else {}

    def read() -> SamplingPolicy:
        return call_policy.read_policy(env=env, env_path=env_file)

    env_file.write_text("MEIVA_SDK_CALL_SAMPLING_ENABLED=true\nMEIVA_SDK_CALL_SAMPLE_EVERY=4\n")
    assert read() == SamplingPolicy(sampling_enabled=True, sample_every=4)
    env_file.write_text("MEIVA_SDK_CALL_SAMPLING_ENABLED=false\nMEIVA_SDK_CALL_SAMPLE_EVERY=7\n")
    assert read() == SamplingPolicy(sampling_enabled=False, sample_every=7)
    env_file.write_text("")
    assert read() == SamplingPolicy()


def test_remote_policy_refreshes_without_waiting_for_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def read() -> SamplingPolicy:
        started.set()
        assert release.wait(5)
        return SamplingPolicy(sampling_enabled=True, sample_every=3)

    cache = _owner(read)
    original = cache.refresh

    def refresh() -> None:
        try:
            original()
        finally:
            finished.set()

    monkeypatch.setattr(cache, "refresh", refresh)
    try:
        assert cache.read() == SamplingPolicy()
        assert started.wait(2)
        assert cache.read() == SamplingPolicy()
    finally:
        release.set()
        assert finished.wait(2)
    assert cache.read() == SamplingPolicy(sampling_enabled=True, sample_every=3)


def test_failed_refresh_preserves_policy_and_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = _owner(
        ScriptedFetch(httpx.ConnectError("gateway unavailable")),
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
    served = {"MEIVA_SDK_CALL_SAMPLING_ENABLED": "true", "MEIVA_SDK_CALL_SAMPLE_EVERY": "2"}
    with serve_json(served) as gateway:
        env = {
            "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
            "MEIVA_GATEWAY_URL": gateway.url,
            "MEIVA_API_TOKEN": "machine-token",
        }
        policy = call_policy.read_policy(env=env, env_path=tmp_path / ".env")
    assert policy == SamplingPolicy(sampling_enabled=True, sample_every=2)
    assert [r.headers["authorization"] for r in gateway.requests] == ["Bearer machine-token"]
    assert [r.path for r in gateway.requests] == ["/api/bootstrap"]


def _http_failure(status: int) -> httpx.HTTPStatusError:
    response = httpx.Response(status, request=httpx.Request("GET", "https://gateway/api/bootstrap"))
    return httpx.HTTPStatusError("bootstrap rejected", request=response.request, response=response)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("offline"),
        httpx.ReadError("connection lost"),
        httpx.ReadTimeout("timed out"),
        httpx.PoolTimeout("busy"),
        httpx.RemoteProtocolError("connection closed"),
        _http_failure(429),
    ],
)
def test_expected_fetch_failure_retains_the_valid_snapshot(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    from base.telemetry import emitter

    def initialize(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("a sampling fetch failure must not initialize an event pipeline")

    monkeypatch.setattr(emitter, "init_telemetry", initialize)
    snapshot = SamplingPolicy(sampling_enabled=True, sample_every=5)
    cache = _owner(ScriptedFetch(error), initial=snapshot)
    cache.refresh()
    assert cache.read() is snapshot


@pytest.mark.parametrize(
    "error",
    [
        _http_failure(401),
        _http_failure(500),
        httpx.LocalProtocolError("invalid request"),
        httpx.UnsupportedProtocol("bad URL"),
        PermissionError("config unreadable"),
        TypeError("invalid config shape"),
        RuntimeError("reader bug"),
    ],
)
def test_unexpected_refresh_error_persists_until_a_valid_snapshot(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    fetch = ScriptedFetch(error)
    cache = _owner(fetch, initial=SamplingPolicy(sample_every=5))
    cache.refresh()
    depths: list[int] = []
    for _ in range(2):
        with pytest.raises(type(error)) as caught:
            cache.read()
        assert caught.value is error
        depths.append(sum(1 for _ in traceback.walk_tb(caught.value.__traceback__)))
    assert depths[0] == depths[1]  # Repeated callers do not accumulate each other's stacks.

    fetch.outcome = httpx.ConnectError("still offline")
    cache.refresh()
    with pytest.raises(type(error)):
        cache.read()  # An outage must not clear an already-invalid configuration.
    recovered = SamplingPolicy(sampling_enabled=True, sample_every=7)
    fetch.outcome = recovered
    cache.refresh()
    assert cache.read() is recovered


def test_invalid_schema_is_not_reported_as_a_retained_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _owner(lambda: SamplingPolicy(sample_every=0))
    cache.refresh()
    with pytest.raises(ValidationError):
        cache.read()


def test_failed_policy_read_still_starts_one_refresh_when_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = TypeError("invalid policy")
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    snapshot = SamplingPolicy(sample_every=9)
    fetches: list[str] = []
    failing = True

    def fetch() -> SamplingPolicy:
        if failing:
            raise error
        fetches.append("fetch")
        started.set()
        assert release.wait(5)
        return snapshot

    cache = _owner(fetch)
    clock = [10.0]
    monkeypatch.setattr(call_policy.time, "monotonic", lambda: clock[0])
    cache.refresh()
    assert cache.next_refresh == 15.0
    failing = False
    original = cache.refresh

    def refresh() -> None:
        try:
            original()
        finally:
            finished.set()

    monkeypatch.setattr(cache, "refresh", refresh)
    clock[0] = 15.0
    try:
        for _ in range(2):
            with pytest.raises(TypeError, match="invalid policy"):
                cache.read()
        assert started.wait(2)
        assert cache.refreshing
        assert fetches == ["fetch"]
    finally:
        release.set()
        assert finished.wait(2)
    assert cache.read() is snapshot
    assert cache.next_refresh == 20.0


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
    cache = _owner(ScriptedFetch(SamplingPolicy()))
    monkeypatch.setitem(sys.modules, "httpx", None)
    cache.refresh()
    with pytest.raises(ImportError, match="httpx"):
        cache.read()


def test_invalid_refresh_dependency_classification_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _owner(ScriptedFetch(SamplingPolicy()))
    monkeypatch.delattr(httpx, "HTTPStatusError")
    cache.refresh()
    with pytest.raises(AttributeError, match="HTTPStatusError"):
        cache.read()
