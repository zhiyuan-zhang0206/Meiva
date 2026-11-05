#!/usr/bin/env python3
# meiva:note
# Read-only host memory sampling: one JSON line per run carrying kernel
# pressure stalls, the user slice's cgroup event counters and the largest
# descendant units with their limits. Nothing here writes to the cgroup tree,
# signals a process or raises an alert; the linked document owns the contract
# and the operator unit that runs it.
# meiva:link scripts/host_ops/docs/host-memory-sample.meiva.okf.md
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict

DEFAULT_LIMIT = 3
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
UNIT_SUFFIXES = (".scope", ".service")


class UnitUsage(TypedDict):
    # meiva:note
    # One cgroup unit's memory: its name, its path under the slice, the current use and its limit (None when unlimited).

    unit: str
    path: str
    current: int
    max: int | None


class CappedUnit(TypedDict):
    # meiva:note
    # A unit that carries a limit: where it sits under the slice, what it is charged now, its
    # limit and its peak. A reader that asks "how much room is left outside the limits" subtracts
    # these from `machine.charged`; the shape stays flat by convention, so a capped unit is never
    # placed inside another capped one and nothing is counted twice.

    path: str
    current: int | None
    max: int
    peak: int | None
    max_events: int | None
    oom_kill: int | None


class MachineUsage(TypedDict):
    # meiva:note
    # Machine-wide memory on the basis the limits are written in, plus the split that decides
    # whether a limit means anything. `charged` is the hierarchy's memory.current, page cache
    # included, so it compares with a unit's own memory.current; but three quarters of it can be
    # dropped on demand, so a parent limit derived from `charged` alone would be set from memory
    # the kernel would happily reclaim. `anon`, `file`, `slab_reclaimable`, `slab_unreclaimable`
    # and `kernel` come from memory.stat and separate the two. `unreclaimable` is the figure a
    # parent budget is written against, computed here rather than by every reader, because the raw
    # columns are nested and adding them up is the mistake the next reader will make: `kernel`
    # already contains every slab, so `anon + slab_unreclaimable + kernel` counts unreclaimable
    # slab twice and reads as roughly double.
    # total/available/swap come from /proc/meminfo and answer a third question -- what a new
    # workload could take without swapping -- so they are reported, not mixed in.

    charged: int | None
    unreclaimable: int | None
    anon: int | None
    file: int | None
    slab: int | None
    slab_reclaimable: int | None
    slab_unreclaimable: int | None
    kernel: int | None
    total: int | None
    available: int | None
    swap_total: int | None
    swap_free: int | None


class SliceUsage(TypedDict):
    current: int | None
    max: int | None
    swap_current: int | None
    swap_max: int | None


class HostSample(TypedDict):
    # meiva:note
    # The JSON line one run appends: pressure, the slice's memory and swap, its events and its largest units.

    ts: str
    pressure: dict[str, dict[str, float]]
    machine: MachineUsage
    slice: SliceUsage
    events: dict[str, int]
    units: list[UnitUsage]
    capped: list[CappedUnit]


# meiva:note
# The parsed flags as a typed namespace. This sampler depends only on the standard library (a
# timer runs it on hosts with no project import path), so the flags are read through the
# namespace's own annotations, which argparse fills with the `type=` converted values, instead of
# through `base.host.boundary.options`.
class _Arguments(argparse.Namespace):
    cgroup_root: str
    pressure: str
    output: str
    limit: int
    max_bytes: int
    echo: bool


def read_pressure(path: Path) -> dict[str, dict[str, float]]:
    pressure: dict[str, dict[str, float]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return pressure
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        values: dict[str, float] = {}
        for item in parts[1:]:
            key, _, raw = item.partition("=")
            try:
                values[key] = float(raw)
            except ValueError:
                continue
        if values:
            pressure[parts[0]] = values
    return pressure


def read_int(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def read_limit(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def read_events(path: Path) -> dict[str, int]:
    events: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return events
    for line in lines:
        key, _, raw = line.partition(" ")
        try:
            events[key] = int(raw)
        except ValueError:
            continue
    return events


# meiva:note
# /proc/meminfo reports kB; every other number in a sample is bytes, so the multiply happens here
# once and the rest of the file never has to know.
def read_meminfo(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not parts:
            continue
        try:
            values[name.strip()] = int(parts[0]) * 1024
        except ValueError:
            continue
    return values


# meiva:note
# The hierarchy root has no memory.current on every host: systemd enables the memory controller in
# the children, and this kernel leaves the root without the file (2026-11-05, ubuntu). The roots'
# direct children partition the machine, so their sum is the same quantity, and reading the file
# first keeps a host that does publish it cheaper to sample.
def machine_sources(machine_root: Path) -> list[Path] | None:
    if (machine_root / "memory.current").exists():
        return [machine_root]
    try:
        children = sorted(child for child in machine_root.iterdir() if child.is_dir())
    except OSError:
        return None
    sources = [child for child in children if (child / "memory.current").exists()]
    return sources or None


def read_stat(path: Path) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        name, _, raw = line.partition(" ")
        try:
            values[name] = int(raw)
        except ValueError:
            continue
    return values


def machine_totals(machine_root: Path) -> tuple[int | None, dict[str, int]]:
    sources = machine_sources(machine_root)
    if sources is None:
        return None, {}
    charged = 0
    stats: dict[str, int] = {}
    for source in sources:
        value = read_int(source / "memory.current")
        if value is None:
            continue
        charged += value
        for key, amount in read_stat(source / "memory.stat").items():
            stats[key] = stats.get(key, 0) + amount
    return charged, stats


# meiva:note
# One named figure so no caller assembles the formula itself. `kernel` already contains every
# slab, so the unreclaimable part is anon plus kernel minus the slab the kernel can drop; adding
# slab_unreclaimable on top double counts it. A stat file that never reported any of the three
# yields None, which says "not measured" rather than a zero that would read as "nothing".
def unreclaimable(stats: dict[str, int]) -> int | None:
    anon = stats.get("anon")
    kernel = stats.get("kernel")
    reclaimable = stats.get("slab_reclaimable")
    if anon is None or kernel is None or reclaimable is None:
        return None
    return anon + kernel - reclaimable


def machine_usage(*, meminfo: Path, machine_root: Path) -> MachineUsage:
    values = read_meminfo(meminfo)
    charged, stats = machine_totals(machine_root)
    return MachineUsage(
        charged=charged,
        unreclaimable=unreclaimable(stats),
        anon=stats.get("anon"),
        file=stats.get("file"),
        slab=stats.get("slab"),
        slab_reclaimable=stats.get("slab_reclaimable"),
        slab_unreclaimable=stats.get("slab_unreclaimable"),
        kernel=stats.get("kernel"),
        total=values.get("MemTotal"),
        available=values.get("MemAvailable"),
        swap_total=values.get("SwapTotal"),
        swap_free=values.get("SwapFree"),
    )


def capped_units(root: Path) -> list[CappedUnit]:
    # meiva:note
    # The slice's own limits are read through `slice`; this walk answers the other half -- which
    # of its units carry a budget right now, and what each has been charged. Peak comes free from
    # the kernel, so a limit set today can be compared with what the unit reached before it.
    # The unit's own `max`/`oom_kill` counters are sampled for the same reason: a transient scope
    # takes its counters with it when it exits, so "was this budget ever hit" is only answerable
    # while the scope is alive and only if it was recorded then.
    found: list[CappedUnit] = []
    for limit_file in root.rglob("memory.max"):
        unit_dir = limit_file.parent
        if not unit_dir.name.endswith(UNIT_SUFFIXES):
            continue
        limit = read_limit(limit_file)
        if limit is None:
            continue
        events = read_events(unit_dir / "memory.events")
        found.append(
            CappedUnit(
                path=unit_dir.relative_to(root).as_posix(),
                current=read_int(unit_dir / "memory.current"),
                max=limit,
                peak=read_int(unit_dir / "memory.peak"),
                max_events=events.get("max"),
                oom_kill=events.get("oom_kill"),
            )
        )
    found.sort(key=lambda item: item["path"])
    return found


def largest_units(root: Path, *, limit: int) -> list[UnitUsage]:
    found: list[UnitUsage] = []
    for current in root.rglob("memory.current"):
        unit_dir = current.parent
        if not unit_dir.name.endswith(UNIT_SUFFIXES):
            continue
        usage = read_int(current)
        if usage is None:
            continue
        found.append(
            UnitUsage(
                unit=unit_dir.name,
                path=unit_dir.relative_to(root).as_posix(),
                current=usage,
                max=read_limit(unit_dir / "memory.max"),
            )
        )
    found.sort(key=lambda item: item["current"], reverse=True)
    return found[:limit]


def sample(
    *,
    pressure: Path,
    cgroup_root: Path,
    limit: int,
    meminfo: Path = Path("/proc/meminfo"),
    machine_root: Path = Path("/sys/fs/cgroup"),
) -> HostSample:
    return HostSample(
        ts=datetime.now(UTC).isoformat(timespec="seconds"),
        pressure=read_pressure(pressure),
        machine=machine_usage(meminfo=meminfo, machine_root=machine_root),
        slice=SliceUsage(
            current=read_int(cgroup_root / "memory.current"),
            max=read_limit(cgroup_root / "memory.max"),
            swap_current=read_int(cgroup_root / "memory.swap.current"),
            swap_max=read_limit(cgroup_root / "memory.swap.max"),
        ),
        events=read_events(cgroup_root / "memory.events"),
        units=largest_units(cgroup_root, limit=limit),
        capped=capped_units(cgroup_root),
    )


def append_line(path: Path, line: str, *, max_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= max_bytes:
        shutil.move(str(path), f"{path}.1")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def default_cgroup_root() -> Path:
    return Path("/sys/fs/cgroup/user.slice") / f"user-{os.getuid()}.slice"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Append one host memory sample as JSON.")
    parser.add_argument("--cgroup-root", default=str(default_cgroup_root()))
    parser.add_argument("--pressure", default="/proc/pressure/memory")
    parser.add_argument(
        "--output",
        default=str(Path.home() / ".meiva" / "logs" / "host-memory.jsonl"),
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--echo", action="store_true")
    args = parser.parse_args(argv, namespace=_Arguments())
    payload = sample(
        pressure=Path(args.pressure),
        cgroup_root=Path(args.cgroup_root),
        limit=args.limit,
    )
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    append_line(Path(args.output).expanduser(), line, max_bytes=args.max_bytes)
    if args.echo:
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
