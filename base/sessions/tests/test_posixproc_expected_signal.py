"""Actual bystander safety when a named service record changes during stop."""

from __future__ import annotations

import signal
import subprocess
from dataclasses import replace
from pathlib import Path

import psutil
import pytest

from base.native_process.kernel_identity import pid_starttime_ticks
from base.sessions import posixproc
from base.sessions.record import SessionRecord


def record_for(pid: int) -> SessionRecord:
    birth = psutil.Process(pid).create_time()
    return SessionRecord(
        pid=pid,
        create_time=birth,
        cmd="isolated CI child",
        cwd="/",
        started_at=birth,
        starttime=pid_starttime_ticks(pid),
    )


def test_replaced_name_never_signals_replacement(
    tmp_path: Path,
) -> None:
    # meiva:note
    # Spawns two fixed /bin/sleep children to verify PID-replacement signal safety.
    argv = ["/bin/sleep", "60"]
    with subprocess.Popen(argv) as old, subprocess.Popen(argv) as replacement:  # noqa: S603
        try:
            expected, new = record_for(old.pid), record_for(replacement.pid)
            path = tmp_path / "meiva-ops.json"
            expected.write(path)
            assert not posixproc.graceful_signal(
                "meiva-ops",
                expected=expected,
                sessions_dir=tmp_path,
                recheck_record=new,
            )
            assert replacement.poll() is None
            assert old.poll() is None
            assert SessionRecord.read(path) == new
        finally:
            # SIGKILL, not terminate(): a shell session's SIGTERM=SIG_IGN is
            # inherited, so the graceful call would leave both children alive.
            old.kill()
            replacement.kill()
            old.wait(timeout=10)
            replacement.wait(timeout=10)


@pytest.mark.parametrize(
    ("stored_tick", "observed_ticks", "birth_delta", "should_signal"),
    [
        (123, (123, 123), -1.0, True),
        (123, (124,), 0.0, False),
        (123, (None,), 0.0, False),
        (123, (123, 124), 0.0, False),
        (None, (), -1.0, True),
        (None, (), -600.0, False),
        (None, (), 0.0, True),
    ],
    ids=[
        "same-ticks-epoch-drift",
        "different-ticks",
        "unreadable-ticks",
        "ticks-changed-before-signal",
        "legacy-birth-drift",
        "legacy-birth-mismatch",
        "legacy-matching-birth",
    ],
)
def test_expected_identity_uses_stable_ticks_before_epoch_birth(
    tmp_path: Path,
    stored_tick: int | None,
    observed_ticks: tuple[int | None, ...],
    birth_delta: float,
    should_signal: bool,
) -> None:
    """A real private child; only the clock observation is simulated.

    A wall-clock step can change btime while /proc start ticks remain stable. Preserve the
    old recorded epoch to reproduce that discrepancy without changing the host
    clock. Unreadable or changed ticks keep refusing delivery; the legacy
    (create_time) guard is bounded by the platform-resolution tolerance, the
    same way a macOS reading moves by whole seconds for one live process.
    """
    # meiva:note
    # Spawns a fixed /bin/sleep child to test start-tick vs epoch-birth identity checks.
    argv = ["/bin/sleep", "60"]
    with subprocess.Popen(argv) as child:  # noqa: S603
        try:
            original = record_for(child.pid)
            expected = replace(
                original, create_time=original.create_time + birth_delta, starttime=stored_tick
            )
            path = tmp_path / "meiva-private-ticks.json"
            expected.write(path)
            assert (
                posixproc.graceful_signal(
                    "meiva-private-ticks",
                    expected=expected,
                    sessions_dir=tmp_path,
                    observed_ticks=observed_ticks,
                )
                is should_signal
            )
            if should_signal:
                assert child.wait(timeout=3) == -signal.SIGTERM
            else:
                assert child.poll() is None
            assert SessionRecord.read(path) == expected
        finally:
            # SIGKILL for the same SIG_IGN-inheritance reason as the test above.
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
