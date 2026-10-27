# meiva:note
# `meiva pty freeze|status|resume`: the host-wide PTY allocation freeze, a marker file in the
# invocation's home. The commands need no gateway and no data plane; `home` is the one the
# invocation settled on.

from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import TextIO

from base.clock import Clock
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.native_process.os_platform import LockTimeoutError
from base.sessions.pty import allocation_freeze


def _print_freeze(
    snapshot: allocation_freeze.PtyAllocationFreeze, *, home: Path, stream: TextIO
) -> None:
    print(f"status={snapshot.status}", file=stream)
    if snapshot.generation is not None:
        print(f"generation={snapshot.generation}", file=stream)
    if snapshot.holder is not None:
        print(f"holder={snapshot.holder}", file=stream)
    if snapshot.reason is not None:
        print(f"reason={snapshot.reason}", file=stream)
    if snapshot.created_at is not None:
        print(f"created_at={snapshot.created_at.isoformat()}", file=stream)
    if snapshot.error is not None:
        print(f"error={snapshot.error}", file=stream)
    print(f"marker={allocation_freeze.state_path(home)}", file=stream)


def cmd_pty_freeze(
    *, holder: str, reason: str, home: Path, clock: Clock, rng: random.Random
) -> int:
    try:
        snapshot = allocation_freeze.freeze(
            holder=holder,
            reason=reason,
            writer=AtomicWriter(SYSTEM_FS),
            home=home,
            clock=clock,
            rng=rng,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"PTY allocation freeze refused: {exc}", file=sys.stderr)
        _print_freeze(allocation_freeze.read(home=home), home=home, stream=sys.stderr)
        return 1
    _print_freeze(snapshot, home=home, stream=sys.stdout)
    return 0


def cmd_pty_status(*, home: Path) -> int:
    snapshot = allocation_freeze.read(home=home)
    _print_freeze(snapshot, home=home, stream=sys.stdout)
    return 1 if snapshot.status == "invalid" else 0


def cmd_pty_resume(*, generation: str, home: Path) -> int:
    try:
        resumed = allocation_freeze.resume(generation, writer=AtomicWriter(SYSTEM_FS), home=home)
    except (
        LockTimeoutError,
        OSError,
        ValueError,
        allocation_freeze.InvalidPtyAllocationFreezeError,
    ) as exc:
        print(f"PTY allocation resume refused: {exc}", file=sys.stderr)
        _print_freeze(allocation_freeze.read(home=home), home=home, stream=sys.stderr)
        return 1
    if not resumed:
        print(
            f"PTY allocation resume refused: generation {generation!r} does not own the freeze",
            file=sys.stderr,
        )
        _print_freeze(allocation_freeze.read(home=home), home=home, stream=sys.stderr)
        return 1
    _print_freeze(allocation_freeze.read(home=home), home=home, stream=sys.stdout)
    return 0
