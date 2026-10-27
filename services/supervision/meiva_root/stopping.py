"""Bounded best-effort stops of root's direct service children.

A live captured leader identifies its process group at signal time. Once the
leader exits, root does not enumerate descendants or reuse its old group number.
"""

from __future__ import annotations

import asyncio
import math
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from base.clock import Clock
from base.deploy.progress_timeout import STOP_KILL_REAP_S
from base.log import logger
from base.native_process.ownership import OwnedProcess
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL, ProcessControl
from base.native_process.root_control.ipc import UnitState
from services.supervision.meiva_root.unit_records import Generation, UnitRuntime

# meiva:note
# A unit's stdout file is the only record of why it died, and it grows for as long as
# the unit lives: `agent-host/output.log` reached 58 MB while nothing rotated it, because
# the file stays open with O_APPEND for the whole generation. Rotation at spawn (the one
# moment nothing holds the file) keeps the reason a unit restarted instead of overwriting
# it. Both numbers are policy on `SupervisorConfig`, kept here with their reason rather
# than inline at the call: 50 MB per file, 3 rotated generations.
UNIT_LOG_MAX_BYTES = 50 * 1024 * 1024
UNIT_LOG_BACKUPS = 3


class UnitStopTimeoutError(RuntimeError):
    # meiva:note
    # The retained direct child did not exit within its bounded stop window.
    pass


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    # meiva:note
    # Timing policy for the supervisor (test-tunable).
    stop_timeout_s: float = 10.0
    # meiva:note
    # Default TERM window: how long a unit may take to close before root kills it.

    stop_kill_reap_s: float = STOP_KILL_REAP_S
    # meiva:note
    # How long root waits for a SIGKILLed child to be reaped before giving up.
    #
    # Short by design: the kernel reaps a killed child promptly, so a longer wait
    # only delays the honest failure report.

    watch_join_timeout_s: float = 0.2
    """Independent bounded observation of retained generation watchers at shutdown."""

    unit_log_max_bytes: int = UNIT_LOG_MAX_BYTES
    unit_log_backups: int = UNIT_LOG_BACKUPS

    def __post_init__(self) -> None:
        if self.unit_log_max_bytes <= 0:
            raise ValueError("unit_log_max_bytes must be positive")
        if self.unit_log_backups < 1:
            raise ValueError("unit_log_backups must be at least 1")
        if not math.isfinite(self.stop_timeout_s) or self.stop_timeout_s <= 0:
            raise ValueError("stop_timeout_s must be finite and positive")
        if not math.isfinite(self.watch_join_timeout_s) or self.watch_join_timeout_s < 0:
            raise ValueError("watch_join_timeout_s must be finite and nonnegative")
        for name in ("stop_kill_reap_s",):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")


# meiva:note
# Attempt the captured leader's group only while that leader still lives.
def signal_owned(
    identity: OwnedProcess,
    *,
    force: bool,
    process_control: ProcessControl = SYSTEM_PROCESS_CONTROL,
) -> bool:
    if not identity.live():
        return False
    signum = signal.SIGKILL if force else signal.SIGTERM
    try:
        group = process_control.getpgid(identity.pid)
        if not identity.live():
            return False
        if group == identity.pid and group != os.getpgrp():
            process_control.killpg(group, signum)
        else:
            process_control.kill(identity.pid, signum)
    except ProcessLookupError:
        return False
    return True


class StoppingMixin:
    """Stop known direct children, without durable custody or descendant discovery."""

    _clock: Clock
    _config: SupervisorConfig
    _process_control: ProcessControl

    async def _stop_unit(
        self, runtime: UnitRuntime, *, force: bool = False, escalate: bool = True
    ) -> None:
        generation = runtime.generation
        if generation is None or generation.proc.returncode is not None:
            runtime.state = UnitState.STOPPED
            return
        await self._stop_generation(runtime, generation, force=force, escalate=escalate)

    async def _stop_generation(
        self,
        runtime: UnitRuntime,
        generation: Generation,
        *,
        force: bool = False,
        escalate: bool = True,
    ) -> None:
        began = self._clock.monotonic()
        generation.closing = True
        identity = generation.identity
        if identity is not None:
            self._signal_owned(identity, force=False)
        window = 0.0 if force else self._stop_window(runtime)
        if await self._wait_exit(generation, window):
            self._log_stopped(runtime, generation, began)
            return
        if not escalate:
            # meiva:note
            # Whole-tree shutdown stays a bounded graceful stop: root is exiting
            # anyway, and a child still closing is reported by custody, not killed.
            raise UnitStopTimeoutError(
                f"unit {runtime.manifest.id} did not stop within its {window:g}s window "
                f"(pid {generation.proc.pid})"
            )
        if identity is None:
            if generation.exited.is_set():
                return
            self._emit_stop_event(
                "root_unit_stop_aborted",
                runtime,
                generation,
                graceful_waited_s=self._clock.monotonic() - began,
                reason="identity_unavailable",
            )
            raise UnitStopTimeoutError(
                f"unit {runtime.manifest.id} outlived its {window:g}s window and root never "
                f"captured its leader identity, so it did not kill it (pid {generation.proc.pid})"
            )
        if not self._signal_owned(identity, force=True):
            if generation.exited.is_set():
                return
            self._emit_stop_event(
                "root_unit_stop_aborted",
                runtime,
                generation,
                graceful_waited_s=self._clock.monotonic() - began,
                reason="identity_changed",
            )
            raise UnitStopTimeoutError(
                f"unit {runtime.manifest.id} outlived its {window:g}s window and its "
                f"leader identity changed, so root did not kill it (pid {generation.proc.pid})"
            )
        self._emit_stop_event(
            "root_unit_stop_escalated",
            runtime,
            generation,
            graceful_waited_s=self._clock.monotonic() - began,
            reason="escaped_term_window",
        )
        if await self._wait_exit(generation, self._config.stop_kill_reap_s):
            self._log_stopped(runtime, generation, began)
            return
        raise UnitStopTimeoutError(
            f"unit {runtime.manifest.id} survived SIGKILL past its "
            f"{self._config.stop_kill_reap_s:g}s reap window (pid {generation.proc.pid})"
        )

    def _log_stopped(self, runtime: UnitRuntime, generation: Generation, began: float) -> None:
        logger.info(
            "unit {unit} generation {generation} stopped in {stopped_in_s}s",
            event="root_unit_stopped",
            unit=runtime.manifest.id,
            generation=generation.seq,
            stopped_in_s=round(self._clock.monotonic() - began, 3),
        )

    @staticmethod
    def _emit_stop_event(
        event: str,
        runtime: UnitRuntime,
        generation: Generation,
        *,
        graceful_waited_s: float,
        reason: str,
    ) -> None:
        try:
            logger.warning(
                "unit {unit} generation {generation}: {reason}",
                event=event,
                unit=runtime.manifest.id,
                generation=generation.seq,
                pid=generation.proc.pid,
                starttime=generation.identity.starttime if generation.identity else None,
                graceful_waited_s=round(graceful_waited_s, 3),
                reason=reason,
            )
        except Exception:
            logger.opt(exception=True).error(
                "unit {}: stop-event fan-out failed ({})", runtime.manifest.id, event
            )

    @staticmethod
    async def _wait_exit(generation: Generation, timeout_s: float) -> bool:
        """Await the existing watcher, without cancelling it on timeout."""
        try:
            await asyncio.wait_for(generation.exited.wait(), timeout_s)
        except TimeoutError:
            return generation.exited.is_set()
        return True

    def _stop_window(self, runtime: UnitRuntime) -> float:
        declared = runtime.manifest.stop_timeout_s
        return self._config.stop_timeout_s if declared is None else declared

    def _signal_owned(
        self,
        identity: OwnedProcess,
        *,
        force: bool,
    ) -> bool:
        return signal_owned(
            identity,
            force=force,
            process_control=self._process_control,
        )


# meiva:note
# Rotation happens at spawn, the one moment no process holds the file: a generation that
# restarts keeps the previous file as `output.log.1` instead of appending to the same
# blob forever. It must never block a unit from starting -- a filesystem error is logged
# and the unit launches into the existing file, because a missing log is cheaper than a
# missing service.
def rotate_unit_log(log_path: Path, *, max_bytes: int, backups: int) -> bool:
    try:
        if log_path.stat().st_size < max_bytes:
            return False
    except FileNotFoundError:
        return False
    try:
        oldest = log_path.with_name(f"{log_path.name}.{backups}")
        oldest.unlink(missing_ok=True)
        for index in range(backups - 1, 0, -1):
            source = log_path.with_name(f"{log_path.name}.{index}")
            if source.exists():
                source.replace(log_path.with_name(f"{log_path.name}.{index + 1}"))
        log_path.replace(log_path.with_name(f"{log_path.name}.1"))
    except OSError as exc:
        logger.warning("unit log rotation skipped for {}: {}", log_path, exc)
        return False
    return True
