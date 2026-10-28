# meiva:note
# How the figures psutil returns become the displayed sample, asserted as values: the used and total
# pair derived from the denominator the percent was measured against, the battery fields merged in
# and which hosts have a battery to read. `test_resource_sample.py` takes real readings.

from __future__ import annotations

import pytest

from base.host.resource.reading import (
    BatteryFields,
    DiskFigures,
    MemoryFigures,
    ResourceSample,
    assemble_sample,
    battery_applies,
)

_GIB = 1024**3


def _assembled(battery: BatteryFields | None = None) -> ResourceSample:
    return assemble_sample(
        ts=1_700_000_000.5,
        cpu_pct=12.5,
        memory=MemoryFigures(16 * _GIB, 4 * _GIB, 75.0),
        disk=DiskFigures(100 * _GIB, 50 * _GIB, 66.7),
        battery={} if battery is None else battery,
    )


def test_memory_used_is_the_total_less_what_is_available() -> None:
    sample = _assembled()

    assert (sample.mem_used_gb, sample.mem_total_gb, sample.mem_pct) == (12.0, 16.0, 75.0)


def test_the_disk_total_is_what_is_used_plus_what_is_free() -> None:
    sample = _assembled()

    assert (sample.disk_used_gb, sample.disk_total_gb, sample.disk_pct) == (100.0, 150.0, 66.7)


def test_the_time_and_the_cpu_reading_pass_through() -> None:
    sample = _assembled()

    assert (sample.ts, sample.cpu_pct) == (1_700_000_000.5, 12.5)


def test_figures_are_rounded_to_two_places() -> None:
    sample = assemble_sample(
        ts=0.0,
        cpu_pct=0.0,
        memory=MemoryFigures(3 * _GIB + _GIB // 3, 0, 100.0),
        disk=DiskFigures(1, 0, 0.0),
        battery={},
    )

    assert sample.mem_total_gb == 3.33
    assert sample.disk_used_gb == 0.0


def test_a_machine_without_a_battery_reads_none_for_every_battery_field() -> None:
    sample = _assembled()

    assert (
        sample.battery_percent,
        sample.battery_power,
        sample.battery_charging,
        sample.battery_remaining_min,
    ) == (None, None, None, None)


def test_battery_fields_are_merged_into_the_sample() -> None:
    sample = _assembled(
        {
            "battery_percent": 80,
            "battery_power": "ac",
            "battery_charging": True,
            "battery_remaining_min": 63,
        }
    )

    assert (
        sample.battery_percent,
        sample.battery_power,
        sample.battery_charging,
        sample.battery_remaining_min,
    ) == (80, "ac", True, 63)


@pytest.mark.parametrize(
    ("platform", "expected"), [("darwin", True), ("linux", False), ("", False), ("Darwin", False)]
)
def test_only_a_macos_host_has_a_battery_to_read(platform: str, expected: bool) -> None:
    assert battery_applies(platform) is expected
