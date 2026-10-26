from __future__ import annotations

import pytest

from base.host.resource_sample import ResourceSample, parse_battery


class TestResourceSample:
    def test_is_frozen(self) -> None:
        s = ResourceSample(
            ts=100.0,
            cpu_pct=1.0,
            mem_used_gb=1.0,
            mem_total_gb=2.0,
            mem_pct=50.0,
            disk_used_gb=1.0,
            disk_total_gb=2.0,
            disk_pct=50.0,
        )
        with pytest.raises(Exception, match="frozen"):
            s.cpu_pct = 2.0

    def test_the_battery_fields_default_to_unknown(self) -> None:
        s = ResourceSample(
            ts=100.0,
            cpu_pct=1.0,
            mem_used_gb=1.0,
            mem_total_gb=2.0,
            mem_pct=50.0,
            disk_used_gb=1.0,
            disk_total_gb=2.0,
            disk_pct=50.0,
        )
        assert (s.battery_percent, s.battery_power, s.battery_charging) == (None, None, None)
        assert s.battery_remaining_min is None


class TestParseBattery:
    PMSET_CHARGING = (
        "Now drawing from 'AC Power'\n"
        " -InternalBattery-0 (id=26607715)\t80%; charging; 1:03 remaining present: true\n"
    )
    PMSET_DISCHARGING = (
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=1)\t23%; discharging; 3:47 remaining present: true\n"
    )
    PMSET_NO_ESTIMATE = (
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=1)\t42%; discharging; (no estimate) present: true\n"
    )
    PMSET_CHARGED = (
        "Now drawing from 'AC Power'\n"
        " -InternalBattery-0 (id=1)\t100%; charged; 0:00 remaining present: true\n"
    )
    PMSET_DESKTOP = "Now drawing from 'AC Power'\n"

    def test_parse_charging_on_ac(self) -> None:
        assert parse_battery(self.PMSET_CHARGING) == {
            "battery_percent": 80,
            "battery_power": "ac",
            "battery_charging": True,
            "battery_remaining_min": 63,
        }

    def test_parse_discharging(self) -> None:
        assert parse_battery(self.PMSET_DISCHARGING) == {
            "battery_percent": 23,
            "battery_power": "battery",
            "battery_charging": False,
            "battery_remaining_min": 227,
        }

    def test_no_estimate_omits_remaining_only(self) -> None:
        assert parse_battery(self.PMSET_NO_ESTIMATE) == {
            "battery_percent": 42,
            "battery_power": "battery",
            "battery_charging": False,
        }

    def test_charged_is_not_charging(self) -> None:
        assert parse_battery(self.PMSET_CHARGED) == {
            "battery_percent": 100,
            "battery_power": "ac",
            "battery_charging": False,
            "battery_remaining_min": 0,
        }

    def test_desktop_reports_nothing(self) -> None:
        assert parse_battery(self.PMSET_DESKTOP) == {}
