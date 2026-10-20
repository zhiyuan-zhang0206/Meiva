#!/usr/bin/env python3
# meiva:note
# A job starts at the concurrency this host can absorb: per-worker peak memory times the
# requested workers, priced against the free space on the data volume, and the requested
# workers are lowered (never refused) when it does not fit. Every lowered or bypassed start
# leaves one line and one JSON event. The linked document owns the formula, the knobs and
# the boundary with the disk and agent-host guards.
# meiva:link scripts/host_ops/docs/admission-run.meiva.okf.md
from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

GIB = 1024**3
DATA_VOLUME = "/System/Volumes/Data"
DEFAULT_RESERVE_GIB = 12.0
DEFAULT_SWAP_MULTIPLIER = 2.0
UNSTABLE_PCT_DELTA = 2.0
POLL_SECONDS = 1.0
INTERPRETERS = frozenset({"uv", "node", "bash", "sh", "zsh", "env"})
# meiva:note
# The flags a job names its own concurrency with. `--eval-jobs` is here because the
# Othello audit scripts spell it that way; a job with a flag we do not know is started
# unchanged and reports that the plan could not be applied, so this list only grows when
# a real launch path needs it.
CONCURRENCY_FLAGS = ("--jobs", "--eval-jobs")
SHORT_FLAGS = ("-j",)


@dataclass(frozen=True)
class DiskReading:
    free_gib: float
    df_pct: float
    unstable: bool


@dataclass(frozen=True)
class Plan:
    jobs: int
    budget_gib: float
    cost_gib: float
    overcommitted: bool


def admission_home() -> Path:
    # meiva:note
    # The table and the event log live under the home the host already uses, so an agent
    # and an operator reading the same machine see one history.
    return Path.home() / ".meiva" / "admission"


def plan(
    requested: int,
    peak_mb: float,
    free_gib: float,
    pressure: int,
    reserve_gib: float,
    multiplier: float,
) -> Plan:
    # meiva:note
    # The one formula. Kernel pressure only downgrades: warn halves the budget, critical
    # starts one worker. Swap never enters it — stale paged-out data occupies disk, not
    # memory demand (2026-10-20: 29.7 GB of swap beside 6.77 GB of process RSS).
    budget = max(0.0, free_gib - reserve_gib)
    cost = max(peak_mb, 0.0) * multiplier / 1024
    if pressure >= 4:
        return Plan(jobs=1, budget_gib=budget, cost_gib=cost, overcommitted=True)
    if pressure >= 2:
        budget /= 2
    fits = int(budget // cost) if cost > 0 else requested
    return Plan(
        jobs=max(1, min(requested, fits)),
        budget_gib=budget,
        cost_gib=cost,
        overcommitted=fits < 1,
    )


def job_key(command: Sequence[str]) -> str:
    # meiva:note
    # The table is keyed by the script a job runs, not by its interpreter or the `uv run`
    # in front of it: a parameter fingerprint would fragment, and `python` alone would
    # merge every Python job on the host into one memory class.
    first = Path(command[0]).name
    if first.startswith("python") or first in INTERPRETERS:
        for token in command[1:]:
            if token.endswith(".py"):
                return Path(token).name
        for token in command[1:]:
            if not token.startswith("-"):
                return Path(token).name
    return first


def _flag_pair(argv: Sequence[str], index: int, flag: str) -> tuple[int, int] | None:
    # meiva:note
    # One flag spelling, two forms (`--jobs 8`, `--jobs=8`); None when this token is not
    # that flag or does not carry a number.
    if argv[index] == flag and index + 1 < len(argv) and argv[index + 1].isdigit():
        return int(argv[index + 1]), index + 1
    prefix = f"{flag}="
    if argv[index].startswith(prefix) and argv[index][len(prefix) :].isdigit():
        return int(argv[index][len(prefix) :]), index
    return None


def requested_jobs(argv: Sequence[str]) -> tuple[int, int | None]:
    # meiva:note
    # The wrapper lowers the concurrency the command already asked for; a command that
    # names none keeps 1, because guessing a higher number would defeat the gate.
    for index in range(len(argv)):
        for flag in CONCURRENCY_FLAGS:
            found = _flag_pair(argv, index, flag)
            if found is not None:
                return found
        for flag in SHORT_FLAGS:
            if argv[index] == flag and index + 1 < len(argv) and argv[index + 1].isdigit():
                return int(argv[index + 1]), index + 1
            if argv[index].startswith(flag) and argv[index][len(flag) :].isdigit():
                return int(argv[index][len(flag) :]), index
    return 1, None


def read_disk(path: str = DATA_VOLUME) -> DiskReading:
    # meiva:note
    # Both subprocess calls run fixed argv (`df -P <volume>`, `ps -g <pid>`, the operator's
    # own job), never a string built from input.
    #
    # Two sources for one quantity: statvfs and `df -P`. A split wider than
    # UNSTABLE_PCT_DELTA points means one of them is mid-refresh, so the caller keeps the
    # requested concurrency and says so instead of acting on a bad read.
    stats = os.statvfs(path)
    total = stats.f_blocks * stats.f_frsize
    free = stats.f_bavail * stats.f_frsize
    statvfs_pct = 100.0 * (total - free) / total if total else 0.0
    try:
        fields = (
            subprocess.run(  # noqa: S603
                ["df", "-P", path], capture_output=True, text=True, check=False
            )
            .stdout.splitlines()[1]
            .split()
        )
        df_pct = float(fields[4].rstrip("%"))
        df_free = int(fields[3]) * 1024
    except (IndexError, ValueError):
        df_pct, df_free = statvfs_pct, free
    return DiskReading(
        free_gib=min(free, df_free) / GIB,
        df_pct=df_pct,
        unstable=abs(df_pct - statvfs_pct) > UNSTABLE_PCT_DELTA,
    )


def read_pressure() -> int:
    # meiva:note
    # The kernel's own three-level classification (1 normal, 2 warn, 4 critical) is the
    # only pressure input; no threshold constant of ours can drift from it.
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        return int(out)
    except ValueError:
        return 1


def swap_line() -> str:
    # meiva:note
    # Printed for the reader, never a criterion: used/total is dominated by stale pages.
    done = subprocess.run(
        ["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, check=False
    )
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def emit(path: Path, kind: str, **fields: object) -> None:
    # meiva:note
    # A failing log never fails the job it describes.
    record: dict[str, object] = {
        "ts": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        "kind": kind,
    }
    record.update(fields)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"admission: cannot write {path}: {exc}", file=sys.stderr)


def read_table(path: Path) -> dict[str, dict[str, object]]:
    # meiva:note
    # An unreadable table is reported and treated as empty: the job still runs, and the
    # next measured run rewrites it.
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"admission: ignoring unreadable table {path}: {exc}", file=sys.stderr)
        return {}


def write_table(path: Path, table: dict[str, dict[str, object]]) -> None:
    # meiva:note
    # Replace in one step so a concurrent reader sees either the old table or the new one.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        print(f"admission: cannot write {path}: {exc}", file=sys.stderr)


def measure_group(pid: int, stop: threading.Event) -> int:
    # meiva:note
    # Summed RSS of the job's whole process group: a multi-worker job's cost is the group,
    # not the parent, and the group is how it is also measured after the run.
    peak = 0
    while not stop.is_set():
        out = subprocess.run(  # noqa: S603
            ["ps", "-g", str(pid), "-o", "rss="], capture_output=True, text=True, check=False
        )
        with contextlib.suppress(ValueError):
            peak = max(peak, sum(int(value) for value in out.stdout.split()))
        stop.wait(POLL_SECONDS)
    return peak


def run_measured(
    command: Sequence[str], *, script: str, jobs: int, requested: int, home: Path
) -> int:
    # meiva:note
    # The measured per-worker peak goes back into the class table, so the next start
    # without --peak-mb prices this host's real job rather than a constant.
    stop = threading.Event()
    peak = {"value": 0}
    process = subprocess.Popen(  # noqa: S603
        list(command), start_new_session=True
    )
    sampler = threading.Thread(
        target=lambda: peak.update(value=measure_group(process.pid, stop)), daemon=True
    )
    sampler.start()
    try:
        code = process.wait()
    finally:
        stop.set()
        sampler.join(timeout=2)
    if peak["value"] and jobs:
        per_worker = peak["value"] / 1024 / jobs
        table = read_table(home / "job-classes.json")
        table[script] = {
            "peak_mb": round(per_worker, 1),
            "peak_total_mb": round(peak["value"] / 1024, 1),
            "jobs": jobs,
            "observed_at": datetime.now(UTC).astimezone().isoformat(timespec="seconds"),
        }
        write_table(home / "job-classes.json", table)
        emit(
            home / "events.jsonl",
            "measured",
            script=script,
            jobs=jobs,
            requested=requested,
            peak_mb=round(per_worker, 1),
            peak_total_mb=round(peak["value"] / 1024, 1),
        )
    return code


def _announce_gate(
    *,
    script: str,
    requested: int,
    peak_mb: float,
    reserve_gib: float,
    multiplier: float,
    disk_reader: Callable[[], DiskReading],
    pressure_reader: Callable[[], int],
    events: Path,
) -> int:
    # meiva:note
    # The whole visible half of the gate: decide, print the one line when it changed, and
    # leave the event. A job that fits prints nothing, so silence stays meaningful.
    disk = disk_reader()
    pressure = pressure_reader()
    outcome = plan(requested, peak_mb, disk.free_gib, pressure, reserve_gib, multiplier)
    if disk.unstable:
        print(
            f"admission: unstable sample — df {disk.df_pct:.1f}% disagrees with statvfs; "
            f"keeping jobs {requested}",
            file=sys.stderr,
        )
        emit(
            events, "unstable", script=script, df_pct=disk.df_pct, free_gib=round(disk.free_gib, 2)
        )
        return requested
    if outcome.jobs == requested:
        return requested
    why = "overcommitted, still starting one" if outcome.overcommitted else "lowered"
    print(
        f"admission: jobs {requested}->{outcome.jobs} ({why}: free {disk.free_gib:.1f} GiB - "
        f"reserve {reserve_gib:.1f} GiB = {outcome.budget_gib:.1f} GiB / "
        f"{outcome.cost_gib:.2f} GiB per worker; pressure {pressure}; {swap_line()})",
        flush=True,
    )
    emit(
        events,
        "overcommitted" if outcome.overcommitted else "adjusted",
        script=script,
        requested=requested,
        jobs=outcome.jobs,
        peak_mb=peak_mb,
        multiplier=multiplier,
        free_disk_gib=round(disk.free_gib, 2),
        budget_gib=round(outcome.budget_gib, 2),
        pressure=pressure,
    )
    return outcome.jobs


def _declared_peak(args: argparse.Namespace, home: Path, script: str) -> float | None:
    # meiva:note
    # A job that never declared its memory is refused rather than given a default: a guessed
    # default is a wrong answer that no later measurement can correct.
    table = read_table(home / "job-classes.json")
    declared = args.peak_mb if args.peak_mb is not None else table.get(script, {}).get("peak_mb")
    if declared is None:
        print(
            f"admission: {script} declares no memory: pass --peak-mb <MiB per worker> once",
            file=sys.stderr,
        )
        return None
    try:
        return float(str(declared))
    except ValueError:
        print(f"admission: {script} has an unreadable peak_mb {declared!r}", file=sys.stderr)
        return None


def _rewrite(
    command: list[str], jobs: int, requested: int, flag_index: int | None, script: str
) -> None:
    # meiva:note
    # Lowering only reaches a command that names its concurrency; one without the flag is
    # started unchanged and told so, instead of silently running the plan nowhere.
    if jobs == requested:
        return
    if flag_index is not None:
        command[flag_index] = str(jobs)
        return
    print(
        f"admission: {script} has no --jobs/-j flag to rewrite; it starts at its own "
        f"concurrency, the plan says {jobs}",
        file=sys.stderr,
    )


def _command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[str]:
    # meiva:note
    # Everything after `--` is the job; without the separator the whole remainder is.
    command = list(args.command[1:] if args.command[:1] == ["--"] else args.command)
    if not command:
        parser.error("no command given (use: admission-run [flags] -- <command...>)")
    return command


def main(
    argv: Sequence[str],
    *,
    disk_reader: Callable[[], DiskReading] = read_disk,
    pressure_reader: Callable[[], int] = read_pressure,
    executor: Callable[[Sequence[str], dict[str, object]], int] | None = None,
    home: Path | None = None,
) -> int:
    # meiva:note
    # Readers, the executor and the home are parameters so a test drives the decision with a
    # chosen host state and no patching; production passes nothing and reads this host.
    parser = argparse.ArgumentParser(prog="admission-run")
    parser.add_argument("--peak-mb", type=float, help="peak RSS per worker, MiB")
    parser.add_argument("--jobs", type=int, help="requested concurrency (else from the command)")
    parser.add_argument("--reserve-gib", type=float, default=DEFAULT_RESERVE_GIB)
    parser.add_argument("--swap-multiplier", type=float, default=DEFAULT_SWAP_MULTIPLIER)
    parser.add_argument("--no-admission", metavar="REASON", help="run unchanged and record why")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, run nothing")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = _command(args, parser)

    resolved_home = home if home is not None else admission_home()
    events = resolved_home / "events.jsonl"
    script = job_key(command)

    if args.no_admission is not None:
        if not args.no_admission.strip():
            print("admission: --no-admission needs a reason", file=sys.stderr)
            return 2
        emit(events, "bypassed", script=script, reason=args.no_admission, command=command)
        return _start(executor, command, script, 1, 1, resolved_home)

    peak_mb = _declared_peak(args, resolved_home, script)
    if peak_mb is None:
        return 2

    named, flag_index = requested_jobs(command)
    requested = args.jobs if args.jobs is not None else named
    jobs = _announce_gate(
        script=script,
        requested=requested,
        peak_mb=peak_mb,
        reserve_gib=args.reserve_gib,
        multiplier=args.swap_multiplier,
        disk_reader=disk_reader,
        pressure_reader=pressure_reader,
        events=events,
    )
    if args.dry_run:
        print(f"admission: dry run — would start {jobs} of {requested} workers", flush=True)
        return 0
    _rewrite(command, jobs, requested, flag_index, script)
    return _start(executor, command, script, jobs, requested, resolved_home)


def _start(
    executor: Callable[[Sequence[str], dict[str, object]], int] | None,
    command: Sequence[str],
    script: str,
    jobs: int,
    requested: int,
    home: Path,
) -> int:
    # meiva:note
    # The one seam a test replaces: everything above decides, this starts the job.
    if executor is not None:
        context: dict[str, object] = {
            "script": script,
            "jobs": jobs,
            "requested": requested,
            "home": home,
        }
        return executor(command, context)
    return run_measured(command, script=script, jobs=jobs, requested=requested, home=home)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
