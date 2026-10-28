# meiva:note
# One live CPU / memory / disk / battery reading for this machine — no history. See
# `base/host/resource/reading.py` for the model and what it keeps; this module reads psutil, the
# clock and `pmset`.

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable

from base.host.resource.reading import (
    BATTERY_READ_ARGV,
    BATTERY_READ_TIMEOUT_S,
    CPU_INTERVAL_S,
    BatteryFields,
    DiskFigures,
    MemoryFigures,
    ResourceSample,
    assemble_sample,
    battery_applies,
    parse_battery,
)
from base.native_process.os_platform import is_macos, primary_disk_path


# meiva:note
# One best-effort battery reading; {} off macOS / on failure / no battery.
#
# Swallows failures by design: the sample degrades to None fields, it does
# not fail the probe (unlike the psutil reads, which propagate).
def battery_sample(
    *,
    platform: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> BatteryFields:
    on_macos = is_macos() if platform is None else battery_applies(platform)
    if not on_macos:
        return {}
    try:
        out = run(
            list(BATTERY_READ_ARGV),
            check=False,
            capture_output=True,
            text=True,
            timeout=BATTERY_READ_TIMEOUT_S,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_battery(out)


# meiva:note
# Read this machine's CPU / memory / primary-disk usage — and, on a macOS
# battery host, its battery state — right now.
#
# Raises whatever psutil raises (including ImportError when it is absent) —
# the status callers decide whether a missing reading degrades the row or
# fails the probe.
def resource_sample(
    *,
    cpu_percent: Callable[[float], float] | None = None,
    battery_fn: Callable[[], BatteryFields] = battery_sample,
) -> ResourceSample:
    import psutil

    read_cpu = psutil.cpu_percent if cpu_percent is None else cpu_percent
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(primary_disk_path())
    return assemble_sample(
        ts=time.time(),
        cpu_pct=read_cpu(CPU_INTERVAL_S),
        memory=MemoryFigures(mem.total, mem.available, mem.percent),
        disk=DiskFigures(disk.used, disk.free, disk.percent),
        battery=battery_fn(),
    )
