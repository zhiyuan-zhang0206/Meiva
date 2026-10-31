# meiva:note
# The disk floor (user ruling relayed by 405, 2026-10-25, task #5630): the answer to "may new
# work start on this host at all". macOS grows the swap pool 1 GiB at a time, every gigabyte
# comes out of the data volume, and past MEIVA_TRACE_DISK_WATERMARK (0.9) newly started agents
# stop recording spans — a memory decision that silently costs observability. The kernel owns
# that growth and offers no userspace hook, so the refusal has to sit upstream, where new
# demand is admitted.
# meiva:link scripts/host_ops/docs/admission-run/disk-floor.meiva.okf.md
from __future__ import annotations

import argparse
import contextlib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal, TextIO

MEM_INFO = "/proc/meminfo"

# meiva:note
# Two lines, not one: a warning and a refusal in the same second leaves an operator no room to
# react. Both are flags of the gate so the line can move without a release.
DEFAULT_DISK_REFUSE_GIB = 23.0
DEFAULT_DISK_WARN_GIB = 24.0

# meiva:note
# The refusal is a state, not a sample (user ruling relayed by 405, 2026-10-30, task #5872).
#
# macOS grows the swap pool one gigabyte at a time and every gigabyte comes out of the data
# volume, so a host near the line moves by exactly the size of the thing the line measures.
# On 2026-10-30 the macmini crossed this line 127 times in one day of ten-second samples while
# nothing about the machine changed; requiring three samples to cross it, and a reset margin
# wider than one swapfile to come back, cut that to 7. The margin must stay larger than the
# quantisation step or the reset fires inside the same wobble that caused the refusal.
DEFAULT_DISK_RESET_MARGIN_GIB = 1.5
DEFAULT_FLOOR_SAMPLES = 3
DEFAULT_FLOOR_SAMPLE_INTERVAL_S = 10.0


def add_arguments(parser: argparse.ArgumentParser) -> None:
    # meiva:note
    # The floor owns its own flags: the line, the warning line, the reset margin, and the probe
    # that decides how many readings a verdict needs. A caller that registers the parser asks
    # for these and gets the whole concern from one module.
    parser.add_argument(
        "--disk-floor-gib",
        type=float,
        default=DEFAULT_DISK_REFUSE_GIB,
        help="refuse to start below this much free space on the data volume (0 disables)",
    )
    parser.add_argument(
        "--disk-warn-gib",
        type=float,
        default=DEFAULT_DISK_WARN_GIB,
        help="warn below this much free space, then start (0 disables)",
    )
    parser.add_argument(
        "--disk-reset-margin-gib",
        type=float,
        default=DEFAULT_DISK_RESET_MARGIN_GIB,
        help="free space above the refuse line that ends a refusal (wider than one swapfile)",
    )
    parser.add_argument(
        "--disk-samples",
        type=int,
        default=DEFAULT_FLOOR_SAMPLES,
        help="consecutive readings below the line needed to refuse (1 = the old single sample)",
    )
    parser.add_argument(
        "--disk-sample-interval",
        type=float,
        default=DEFAULT_FLOOR_SAMPLE_INTERVAL_S,
        help="seconds between those readings",
    )


def read_state(path: Path) -> bool:
    # meiva:note
    # The refusal outlives the process that made it: a host that refused stays refused while
    # it sits between the two lines, so the next invocation cannot reopen on the same wobble.
    # An unreadable state reads as "not refused" — the gate then has to cross the line three
    # times again, which is the safe direction (one more probe, never a silently blocked host).
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, json.JSONDecodeError) as exc:
        print(f"admission: ignoring unreadable floor state {path}: {exc}", file=sys.stderr)
        return False
    return bool(state.get("refused", False))


def write_state(path: Path, *, refused: bool, free_gib: float) -> None:
    # meiva:note
    # Same one-step replace as the peak table, and a failing write never fails the job it
    # describes: the worst case is a refusal that has to be re-earned by three fresh samples.
    # The state carries no timestamp and needs none: one reading at the reset line clears it.
    record = {"refused": refused, "free_gib": round(free_gib, 2)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        print(f"admission: cannot write {path}: {exc}", file=sys.stderr)


def sample(
    first_free_gib: float,
    *,
    reader: Callable[[], float | None],
    refuse_gib: float,
    reset_gib: float,
    samples: int,
    interval: float,
    sleeper: Callable[[float], None],
) -> tuple[list[float], float]:
    # meiva:note
    # A host comfortably above the line is not near the edge, so it costs one read and no
    # waiting; only a reading inside the band is probed `samples` times, `interval` apart. A
    # reader that stops answering ends the probe with what it has — fewer samples can warn but
    # can never refuse, so a flaky probe cannot block work.
    readings = [first_free_gib]
    latest = first_free_gib
    if refuse_gib <= 0 or first_free_gib >= reset_gib or samples <= 1:
        return readings, latest
    for _ in range(samples - 1):
        sleeper(interval)
        following = reader()
        if following is None:
            break
        latest = following
        readings.append(following)
    return readings, latest


def floor_verdict(
    readings: Sequence[float],
    *,
    refuse_gib: float,
    warn_gib: float,
    reset_gib: float,
    refused_before: bool,
    required_samples: int = DEFAULT_FLOOR_SAMPLES,
) -> Literal["refuse", "warn", "ok"]:
    # meiva:note
    # A verdict, not a plan: a refusal must not be tuned by `--peak-mb` or `--swap-multiplier`,
    # which price the job rather than the machine. Zero disables the line (the reverse check
    # runs the same host with the floor out of the way).
    #
    # Three rules, in this order — enter, leave, stay:
    #   enter  a host refuses only when every reading is below the line, so one dip is not a
    #          verdict and a reader that dies mid-way cannot refuse on a single sample;
    #   leave  one reading at or above the reset line ends the refusal, and the warning line
    #          decides whether the restart is quiet;
    #   stay   between the two lines the previous answer holds, which is the hysteresis: a
    #          refused host does not reopen on the same wobble that closed it.
    if refuse_gib <= 0:
        return "ok"
    last = readings[-1]
    if refused_before:
        if last >= reset_gib:
            return "warn" if warn_gib > 0 and last < warn_gib else "ok"
        return "refuse"
    if len(readings) >= max(1, required_samples) and all(one < refuse_gib for one in readings):
        return "refuse"
    if warn_gib > 0 and last < warn_gib:
        return "warn"
    return "ok"


def floor_line(
    verdict: str,
    *,
    script: str,
    free_gib: float,
    detail: str,
    swap: str,
    pressure: int,
    floor_gib: float,
) -> str:
    # meiva:note
    # The observable half: what the machine has, read from the data volume, the line it crossed
    # and the one thing an operator can do about it. The machine itself is not named — this file
    # may not read the platform (locality rule), and both the line and the event land on the host
    # that refused, so its identity is where they were written.
    where = f"{free_gib:.1f} GiB free ({detail}); {swap}; pressure {pressure}"
    if verdict == "refuse":
        return (
            f"admission: refusing to start {script} — {where} < {floor_gib:.1f} GiB floor. "
            "The kernel grows the swap pool 1 GiB at a time and past the trace watermark new "
            "agents stop recording spans. Free disk space, or pass --no-admission <reason>."
        )
    return f"admission: warning {script} — {where} < {floor_gib:.1f} GiB; starting anyway"


def swap_line(
    platform: str,
    sysctl: Sequence[str] = ("sysctl", "-n", "vm.swapusage"),
) -> str:
    # meiva:note
    # Printed for the reader, never a criterion: used/total is dominated by stale pages.
    # `platform` is required rather than defaulted from `sys.platform`: this module may not
    # read the platform, and the caller that does already owns that debt.
    if platform != "darwin":
        values: dict[str, float] = {}
        with contextlib.suppress(OSError):
            for line in Path(MEM_INFO).read_text(encoding="utf-8").splitlines():
                if line.startswith(("SwapTotal:", "SwapFree:")):
                    key, _, rest = line.partition(":")
                    values[key] = int(rest.split()[0]) / (1024 * 1024)
        if "SwapTotal" in values:
            used = values["SwapTotal"] - values.get("SwapFree", 0.0)
            return f"swap {used:.1f}/{values['SwapTotal']:.1f} GiB"
        return "swap unknown"
    try:
        done = subprocess.run(  # noqa: S603
            list(sysctl), capture_output=True, text=True, check=False
        )
    except OSError:
        return "unknown"
    return done.stdout.strip() if done.returncode == 0 else "unknown"


def report(
    verdict: str,
    *,
    free_gib: float,
    detail: str,
    source: str,
    script: str,
    floor_gib: float,
    pressure: int,
    platform_name: str,
    readings: Sequence[float],
    refused_before: bool,
    events: Path,
    emit: Callable[..., None],
    stream: TextIO | None = None,
) -> None:
    # meiva:note
    # The observable half, kept beside the verdict that produced it: the line an operator reads
    # and one event carrying the readings that earned it. `emit` and `stream` come in as
    # parameters and `stream` resolves at call time (a default bound at import would keep the
    # stderr of the process that imported it), so this module owns what a refusal says without
    # owning where it is written.
    print(
        floor_line(
            verdict,
            script=script,
            free_gib=free_gib,
            detail=detail,
            swap=swap_line(platform_name),
            pressure=pressure,
            floor_gib=floor_gib,
        ),
        file=stream if stream is not None else sys.stderr,
        flush=True,
    )
    emit(
        events,
        "refused" if verdict == "refuse" else "warned",
        script=script,
        free_gib=round(free_gib, 2),
        source=source,
        detail=detail,
        floor_gib=floor_gib,
        pressure=pressure,
        readings=len(readings),
        lowest_gib=round(min(readings), 2),
        refused_before=refused_before,
    )
