# meiva:note
# One live CPU / memory / disk / battery reading for this machine — no history. See
# `base/host/resource/reading.py` for the model and what it keeps; this module reads psutil and
# `pmset`, and stamps the reading with the instant its caller names.

from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Protocol

from base.host.resource.reading import (
    BATTERY_READ_ARGV,
    BATTERY_READ_TIMEOUT_S,
    CPU_INTERVAL_S,
    BatteryFields,
    DiskFigures,
    MemoryFigures,
    ResourceSample,
    assemble_sample,
    parse_battery,
)
from base.native_process.os_platform import primary_disk_path


# meiva:note
# What is read of `psutil.virtual_memory()`; psutil types its fields `Any`, so a parameter typed
# with this view takes the answer without naming the `Any`.
class _VirtualMemory(Protocol):
    @property
    def total(self) -> int: ...

    @property
    def available(self) -> int: ...

    @property
    def percent(self) -> float: ...


# meiva:note
# What is read of `psutil.disk_usage()`, as `_VirtualMemory` is for memory.
class _DiskUsage(Protocol):
    @property
    def used(self) -> int: ...

    @property
    def free(self) -> int: ...

    @property
    def percent(self) -> float: ...


def _figures(mem: _VirtualMemory, disk: _DiskUsage) -> tuple[MemoryFigures, DiskFigures]:
    return (
        MemoryFigures(mem.total, mem.available, mem.percent),
        DiskFigures(disk.used, disk.free, disk.percent),
    )


# meiva:note
# One best-effort battery reading; {} off macOS / on failure / no battery.
#
# Swallows failures by design: the sample degrades to None fields, it does
# not fail the probe (unlike the psutil reads, which propagate).
def battery_sample(
    *,
    platform: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> BatteryFields:
    if platform != "darwin":
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
    now: float,
    battery: BatteryFields,
    cpu_percent: Callable[[float], float] | None = None,
) -> ResourceSample:
    import psutil

    read_cpu = psutil.cpu_percent if cpu_percent is None else cpu_percent
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(primary_disk_path())
    memory, disk_figures = _figures(mem, disk)
    return assemble_sample(
        ts=now,
        cpu_pct=read_cpu(CPU_INTERVAL_S),
        memory=memory,
        disk=disk_figures,
        battery=battery,
    )
