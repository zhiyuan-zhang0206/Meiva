# meiva:note
# The disk floor (user ruling relayed by 405, 2026-10-25, task #5630): the answer to "may new
# work start on this host at all". macOS grows the swap pool 1 GiB at a time, every gigabyte
# comes out of the data volume, and past MEIVA_TRACE_DISK_WATERMARK (0.9) newly started agents
# stop recording spans — a memory decision that silently costs observability. The kernel owns
# that growth and offers no userspace hook, so the refusal has to sit upstream, where new
# demand is admitted.
# meiva:link scripts/host_ops/docs/admission-run/disk-floor.meiva.okf.md
from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

MEM_INFO = "/proc/meminfo"

# meiva:note
# Two lines, not one: a warning and a refusal in the same second leaves an operator no room to
# react. Both are flags of the gate so the line can move without a release.
DEFAULT_DISK_REFUSE_GIB = 23.0
DEFAULT_DISK_WARN_GIB = 24.0


def disk_floor(
    free_gib: float, *, refuse_gib: float, warn_gib: float
) -> Literal["refuse", "warn", "ok"]:
    # meiva:note
    # A verdict, not a plan: a refusal must not be tuned by `--peak-mb` or `--swap-multiplier`,
    # which price the job rather than the machine. Zero disables a line (the reverse check runs
    # the same host with the floor out of the way).
    if refuse_gib > 0 and free_gib < refuse_gib:
        return "refuse"
    if warn_gib > 0 and free_gib < warn_gib:
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
