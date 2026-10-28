# meiva:note
# One live CPU / memory / disk / battery reading for this machine, as values: the model of the
# reading, how `pmset -g batt` text becomes battery fields and how the figures psutil returns
# become the displayed sample. `sample.py` reads psutil, the clock and `pmset`.
#
# The host metrics live in Prometheus, scraped by the per-machine OTel Collector sidecar's
# `host_metrics` receiver, and Prometheus is the ONE time-series store: a second retained history
# would be a second answer to "what was the CPU on machine X" that drifts from the first. What
# survives here is the degraded answer, the reading `meiva status` and the status page must still
# show on a deployment where the LGTM backend is down or was never deployed. It is a live one-shot
# sample and nothing is kept between calls.

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel, ConfigDict

# meiva:note
# psutil's non-blocking cpu_percent reports the average since the PREVIOUS call
# in the same process, which is meaningless for a stateless one-shot: the first
# call returns 0.0 and later ones cover whatever interval the last poller left
# behind. A short blocking measurement is self-contained; the callers already
# run this off the event loop.
CPU_INTERVAL_S = 0.1

# meiva:note
# The command that reads the battery of a macOS host.
BATTERY_READ_ARGV = ("/usr/bin/pmset", "-g", "batt")

# meiva:note
# task #3696 exception inventory: a protective bound on one best-effort local
# read (`pmset -g batt`) so a hung command cannot stall the status probe that
# consumes this sample. The probe's own 8s budget
# (MEIVA_STATUS_PROBE_TIMEOUT_SECONDS) sets the ceiling; not an operator knob.
BATTERY_READ_TIMEOUT_S = 2.0


# meiva:note
# The battery keys `parse_battery` may emit; an absent key means unknown.
class BatteryFields(TypedDict, total=False):
    battery_percent: int
    battery_power: Literal["ac", "battery"]
    battery_charging: bool
    battery_remaining_min: int


# meiva:note
# This machine's CPU / memory / disk / battery at the moment it was read: `ts` is the unix
# timestamp, the percentages run 0-100, and the battery fields (macOS laptops, task #3743) are None
# when the machine has no battery or the reading failed — never a fabricated default. The
# description is the one the OpenAPI schema carries for the model, set here so the schema does not
# depend on a docstring.
class ResourceSample(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        json_schema_extra={
            "description": "This machine's CPU / memory / disk / battery at the moment it was read."
        },
    )

    ts: float
    cpu_pct: float
    mem_used_gb: float
    mem_total_gb: float
    mem_pct: float
    disk_used_gb: float
    disk_total_gb: float
    disk_pct: float
    battery_percent: int | None = None
    battery_power: Literal["ac", "battery"] | None = None
    battery_charging: bool | None = None
    battery_remaining_min: int | None = None


# meiva:note
# Whether a host of this platform name has a battery reading to take: only macOS does.
def battery_applies(platform: str) -> bool:
    return platform == "darwin"


# meiva:note
# Parse `pmset -g batt` text into the battery fields; {} when no battery.
#
# Sample input (a battery-powered macOS host on AC):
#     Now drawing from 'AC Power'
#      -InternalBattery-0 (id=26607715)\t80%; charging; 1:03 remaining present: true
#
# Desktops print only the first line — no battery, so every field stays None. "no estimate" (no
# time-to-empty yet) omits battery_remaining_min alone; an unknown state string omits
# battery_charging rather than guessing.
def parse_battery(out: str) -> BatteryFields:
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


# meiva:note
# The memory figures psutil returned: the bytes of RAM, the bytes available (reclaimable cache
# counts) and the percent psutil reports.
@dataclass(frozen=True)
class MemoryFigures:
    total: int
    available: int
    percent: float


# meiva:note
# The primary disk's figures psutil returned: the bytes used and free and the percent psutil reports.
@dataclass(frozen=True)
class DiskFigures:
    used: int
    free: int
    percent: float


# meiva:note
# The displayed sample from the figures psutil returned.
#
# psutil's `percent` is the canonical fullness/pressure figure, but it is
# measured against a *different* denominator than the naive `.total`, so
# `used / total` disagreed with the shown percent (disk read 69% next to
# an 84% bar, memory 34% next to 80%). Derive the displayed used/total
# pair from that same denominator so the panel is self-consistent —
# shown_used / shown_total == shown_percent:
#   * disk.percent == used / (used + free)  (== `df` Capacity; the
#     root-reserved blocks that pad `disk.total` are excluded), so the
#     shown total is used + free, the space actually available to us.
#   * mem.percent == (total - available) / total, where `available`
#     counts reclaimable cache; `mem.used` alone undercounts on macOS, so
#     the shown used is total - available (real pressure), total is RAM.
def assemble_sample(
    *,
    ts: float,
    cpu_pct: float,
    memory: MemoryFigures,
    disk: DiskFigures,
    battery: BatteryFields,
) -> ResourceSample:
    disk_total = disk.used + disk.free
    mem_used = memory.total - memory.available
    return ResourceSample(
        ts=ts,
        cpu_pct=cpu_pct,
        mem_used_gb=round(mem_used / (1024**3), 2),
        mem_total_gb=round(memory.total / (1024**3), 2),
        mem_pct=memory.percent,
        disk_used_gb=round(disk.used / (1024**3), 2),
        disk_total_gb=round(disk_total / (1024**3), 2),
        disk_pct=disk.percent,
        **battery,
    )
