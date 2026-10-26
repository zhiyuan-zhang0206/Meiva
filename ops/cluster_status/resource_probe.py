# meiva:note
# The effect shell of the live resource reading: psutil for CPU, memory and the disk, `pmset` for
# the battery. `base.host.resource_sample` owns the shape (`ResourceSample`, `parse_battery`); this
# module only reads the machine. The instant, the disk path and the battery reading are values
# the caller passes in, so a test hands in the ones it wants and the reading itself is a real
# psutil call. A psutil failure propagates, because the status callers decide whether a missing
# reading degrades the row or fails the probe; only the best-effort `pmset` read degrades to no
# battery fields.

from __future__ import annotations

import subprocess
from collections.abc import Callable

from base.host.resource_sample import BatteryFields, ResourceSample, parse_battery
from base.native_process.os_platform import is_macos, primary_disk_path

# meiva:note
# psutil's non-blocking cpu_percent reports the average since the PREVIOUS call
# in the same process, which is meaningless for a stateless one-shot: the first
# call returns 0.0 and later ones cover whatever interval the last poller left
# behind. A short blocking measurement is self-contained; the callers already
# run this off the event loop.
CPU_INTERVAL_S = 0.1

# meiva:note
# task #3696 exception inventory: a protective bound on one best-effort local
# read (`pmset -g batt`) so a hung command cannot stall the status probe that
# consumes this sample. The probe's own 8s budget
# (MEIVA_STATUS_PROBE_TIMEOUT_SECONDS) sets the ceiling; not an operator knob.
_BATTERY_READ_TIMEOUT_S = 2.0


# meiva:note
# One best-effort battery reading; `{}` off macOS, on failure, or when the machine has no battery.
# The failure is swallowed by design: the sample degrades to None fields and does not fail the
# probe, unlike the psutil reads. Only the read's own failures are swallowed (a missing or
# non-executable `pmset`, a timeout); anything else propagates.
def battery_sample(
    *, platform: str, run: Callable[..., subprocess.CompletedProcess[str]]
) -> BatteryFields:
    if platform != "darwin":
        return {}
    try:
        out = run(
            ["/usr/bin/pmset", "-g", "batt"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_BATTERY_READ_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_battery(out)


# meiva:note
# Read this machine's CPU, memory and `disk_path` usage right now and stamp it with `ts`.
# `disk_path` names the filesystem the disk figures describe (on macOS the data volume, not the
# sealed system volume; see `primary_disk_path`).
def resource_sample(*, ts: float, disk_path: str, battery: BatteryFields) -> ResourceSample:
    import psutil

    cpu_pct = psutil.cpu_percent(CPU_INTERVAL_S)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(disk_path)

    # meiva:note
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
    disk_total = disk.used + disk.free
    mem_used = mem.total - mem.available

    return ResourceSample(
        ts=ts,
        cpu_pct=cpu_pct,
        mem_used_gb=round(mem_used / (1024**3), 2),
        mem_total_gb=round(mem.total / (1024**3), 2),
        mem_pct=mem.percent,
        disk_used_gb=round(disk.used / (1024**3), 2),
        disk_total_gb=round(disk_total / (1024**3), 2),
        disk_pct=disk.percent,
        **battery,
    )


# meiva:note
# The reading every status surface takes: this machine's primary disk, and the battery on a
# macOS host. The one place the platform decides whether `pmset` is asked.
def host_resource_sample(*, ts: float) -> ResourceSample:
    platform = "darwin" if is_macos() else "other"
    battery = battery_sample(platform=platform, run=subprocess.run)
    return resource_sample(ts=ts, disk_path=primary_disk_path(), battery=battery)
