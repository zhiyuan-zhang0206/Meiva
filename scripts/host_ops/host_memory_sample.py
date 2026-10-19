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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_LIMIT = 3
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
UNIT_SUFFIXES = (".scope", ".service")


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


def largest_units(root: Path, *, limit: int) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for current in root.rglob("memory.current"):
        unit_dir = current.parent
        if not unit_dir.name.endswith(UNIT_SUFFIXES):
            continue
        usage = read_int(current)
        if usage is None:
            continue
        found.append(
            {
                "unit": unit_dir.name,
                "path": unit_dir.relative_to(root).as_posix(),
                "current": usage,
                "max": read_limit(unit_dir / "memory.max"),
            }
        )
    found.sort(key=lambda item: item["current"], reverse=True)
    return found[:limit]


def sample(*, pressure: Path, cgroup_root: Path, limit: int) -> dict[str, Any]:
    return {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "pressure": read_pressure(pressure),
        "slice": {
            "current": read_int(cgroup_root / "memory.current"),
            "max": read_limit(cgroup_root / "memory.max"),
            "swap_current": read_int(cgroup_root / "memory.swap.current"),
            "swap_max": read_limit(cgroup_root / "memory.swap.max"),
        },
        "events": read_events(cgroup_root / "memory.events"),
        "units": largest_units(cgroup_root, limit=limit),
    }


def append_line(path: Path, line: str, *, max_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= max_bytes:
        shutil.move(str(path), f"{path}.1")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def default_cgroup_root() -> Path:
    return Path("/sys/fs/cgroup/user.slice") / f"user-{os.getuid()}.slice"


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)
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
    raise SystemExit(main())
