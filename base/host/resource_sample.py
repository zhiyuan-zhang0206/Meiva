"""One live CPU / memory / disk / battery reading for this machine — no history.

The host metrics live in
Prometheus, scraped by the per-machine OTel Collector sidecar's `host_metrics`
receiver, and Prometheus is the ONE time-series store: a second retained
history would be a second answer to "what was the CPU on machine X" that drifts
from the first.

What survives here is the degraded answer — the reading `meiva status` and the
status page must still show on a deployment where the LGTM backend is down or
was never deployed (observability is optional; `meiva lgtm off` is a supported
state). It is a live one-shot sample and nothing is kept between calls, so
there is no cadence to keep, nothing to lose on restart, and no drift.
"""

from __future__ import annotations

import re
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict


# meiva:note
# The battery keys `parse_battery` may emit; an absent key means unknown.
class BatteryFields(TypedDict, total=False):
    battery_percent: int
    battery_power: Literal["ac", "battery"]
    battery_charging: bool
    battery_remaining_min: int


class ResourceSample(BaseModel):
    """This machine's CPU / memory / disk / battery at the moment it was read."""

    model_config = ConfigDict(frozen=True)

    ts: float  # unix timestamp
    cpu_pct: float  # CPU usage percent (0-100)
    mem_used_gb: float
    mem_total_gb: float
    mem_pct: float  # memory usage percent
    disk_used_gb: float
    disk_total_gb: float
    disk_pct: float  # disk usage percent
    # Battery telemetry (macOS laptops, task #3743). None when the machine has
    # no battery or the reading failed — never a fabricated default.
    battery_percent: int | None = None
    battery_power: Literal["ac", "battery"] | None = None
    battery_charging: bool | None = None
    battery_remaining_min: int | None = None


def parse_battery(out: str) -> BatteryFields:
    """Parse `pmset -g batt` text into the battery fields; {} when no battery.

    Sample input (a battery-powered macOS host on AC):
        Now drawing from 'AC Power'
         -InternalBattery-0 (id=26607715)\t80%; charging; 1:03 remaining present: true

    Desktops print only the first line — no battery, so every field stays None.
    "no estimate" (no time-to-empty yet) omits battery_remaining_min alone; an
    unknown state string omits battery_charging rather than guessing.
    """
    m = re.search(r"(\d+)%;\s*([^;\n]+)", out)
    if m is None:
        return {}
    fields: BatteryFields = {"battery_percent": int(m.group(1))}
    state = m.group(2).strip().lower()
    if state in ("charging", "finishing charge"):
        fields["battery_charging"] = True
    elif state in ("discharging", "charged", "ac attached", "not charging"):
        fields["battery_charging"] = False
    p = re.search(r"drawing from '([^']+)'", out)
    if p:
        if p.group(1) == "AC Power":
            fields["battery_power"] = "ac"
        elif p.group(1) == "Battery Power":
            fields["battery_power"] = "battery"
    r = re.search(r"(\d+):(\d{2})\s*remaining", out)
    if r:
        fields["battery_remaining_min"] = int(r.group(1)) * 60 + int(r.group(2))
    return fields
