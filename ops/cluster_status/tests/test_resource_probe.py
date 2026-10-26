from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from base.host.resource_sample import ResourceSample, parse_battery
from ops.cluster_status.resource_probe import (
    CPU_INTERVAL_S,
    battery_sample,
    host_resource_sample,
    resource_sample,
)

_PMSET_CHARGING = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=26607715)\t80%; charging; 1:03 remaining present: true\n"
)
_PMSET_DISCHARGING = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=1)\t23%; discharging; 3:47 remaining present: true\n"
)


class TestResourceSample:
    def test_returns_a_self_consistent_reading(self) -> None:
        s = resource_sample(ts=100.0, disk_path="/", battery={})
        assert isinstance(s, ResourceSample)
        assert s.cpu_pct >= 0.0
        assert 0.0 <= s.mem_pct <= 100.0
        assert 0.0 <= s.disk_pct <= 100.0
        assert s.mem_used_gb <= s.mem_total_gb
        assert s.disk_used_gb <= s.disk_total_gb
        assert s.mem_total_gb > 0

    def test_the_instant_is_the_one_passed_in(self) -> None:
        assert resource_sample(ts=1234.5, disk_path="/", battery={}).ts == 1234.5
        assert host_resource_sample(ts=99.25).ts == 99.25

    def test_cpu_is_measured_not_inherited(self) -> None:
        started = time.perf_counter()
        resource_sample(ts=100.0, disk_path="/", battery={})
        assert time.perf_counter() - started >= CPU_INTERVAL_S * 0.9

    def test_the_disk_is_the_path_passed_in(self, tmp_path: Path) -> None:
        s = resource_sample(ts=100.0, disk_path=str(tmp_path), battery={})
        assert s.disk_total_gb > 0
        with pytest.raises(FileNotFoundError):
            resource_sample(ts=100.0, disk_path=str(tmp_path / "missing"), battery={})

    def test_sample_carries_battery_fields(self) -> None:
        s = resource_sample(ts=100.0, disk_path="/", battery=parse_battery(_PMSET_CHARGING))
        assert s.battery_percent == 80
        assert s.battery_power == "ac"
        assert s.battery_charging is True
        assert s.battery_remaining_min == 63


class TestBatterySample:
    def test_off_macos_never_asks_pmset(self) -> None:
        def _never_run(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise AssertionError("pmset must not run off macOS")

        assert battery_sample(platform="linux", run=_never_run) == {}

    def test_macos_reads_the_battery_through_run(self) -> None:
        def _pmset(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout=_PMSET_DISCHARGING, stderr=""
            )

        assert battery_sample(platform="darwin", run=_pmset) == parse_battery(_PMSET_DISCHARGING)

    @pytest.mark.parametrize(
        "failure",
        [subprocess.TimeoutExpired("pmset", 2), FileNotFoundError("pmset")],
    )
    def test_read_failure_degrades_to_no_fields(self, failure: Exception) -> None:
        def _fail(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise failure

        assert battery_sample(platform="darwin", run=_fail) == {}

    def test_an_unexpected_failure_is_not_swallowed(self) -> None:
        def _bug(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
            raise ValueError("not a read failure")

        with pytest.raises(ValueError, match="not a read failure"):
            battery_sample(platform="darwin", run=_bug)
