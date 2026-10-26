import contextlib
import threading
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import httpx
import pytest

from base.agents.sdk.call_policy import SamplingPolicy, SamplingPolicyOwner, SamplingSource
from base.agents.sdk.tests.sampling_owners import gateway_sampling_owner
from base.clock.tests.seeded_rng import seeded_rng
from base.host.net.tests.local_http import serve_json


# meiva:note
# An environment that cannot be read: any lookup is the operator interrupting the process.
class _InterruptedEnv(Mapping[str, str]):
    def __getitem__(self, key: str) -> str:
        raise KeyboardInterrupt("refresh interrupted")

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


def test_stop_retains_late_failure_and_actual_worker(tmp_path: Path) -> None:
    gate = threading.Event()
    with serve_json({}, status=500, gate=gate) as gateway:
        owner = gateway_sampling_owner(gateway.url, tmp_path / ".env")
        owner.read()
        assert gateway.arrived.wait(2)
        worker = owner.worker
        assert worker is not None
        started = time.monotonic()
        assert owner.stop(0.02) is False
        assert time.monotonic() - started < 1
        assert owner.worker is worker and worker.thread.is_alive()
        with pytest.raises(RuntimeError, match="has stopped"):
            owner.read()
        gate.set()
        assert worker.completed.wait(5)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        owner.stop()
    assert owner.error is not None and caught.value is owner.error[0]
    assert not worker.thread.is_alive()


def test_stop_precedes_success_receipt(tmp_path: Path) -> None:
    gate = threading.Event()
    with serve_json({}, status=500, gate=gate) as gateway:
        owner = gateway_sampling_owner(gateway.url, tmp_path / ".env")
        owner.read()
        assert gateway.arrived.wait(2)
        worker = owner.worker
        assert worker is not None
        gate.set()
        assert worker.completed.wait(5)
    receipts: list[str] = []
    with pytest.raises(httpx.HTTPStatusError) as caught:
        owner.stop()
        receipts.append("done")
    assert owner.error is not None and caught.value is owner.error[0]
    assert receipts == []


def test_stop_collects_refresh_boundary_failure(tmp_path: Path) -> None:
    owner = SamplingPolicyOwner(
        initial=SamplingPolicy(),
        source=SamplingSource(
            env=_InterruptedEnv(), env_path=tmp_path / ".env", refresh_seconds=0.0
        ),
        rng=seeded_rng(),
    )
    with contextlib.suppress(KeyboardInterrupt):
        owner.read()
    worker = owner.worker
    assert worker is not None and worker.completed.wait(2)
    original = worker.error
    assert isinstance(original, KeyboardInterrupt)
    with pytest.raises(KeyboardInterrupt) as read_failure:
        owner.read()
    assert read_failure.value is original and owner.worker is worker
    with pytest.raises(KeyboardInterrupt) as caught:
        owner.stop()
    assert caught.value is original
    assert worker.error is original and not worker.thread.is_alive()
