# meiva:note
# Reclaim the processes a previous root left behind before spawning new ones.
#
# Root stops recorded processes from previous generations before it spawns
# replacements, using standard pidfile and process group cleanup.
# meiva:link services/supervision/meiva_root/docs/meiva_root.meiva.okf.md

from __future__ import annotations

import logging
import signal
from pathlib import Path

from base.clock import Clock
from base.host.proc import process_alive
from base.log import logger
from base.native_process.process_control import ProcessControl
from services.supervision.meiva_root import unit_ledger
from services.supervision.meiva_root.stopping import StoppingMixin, SupervisorConfig
from services.supervision.meiva_root.unit_records import UnitRuntime

_log = logging.getLogger(__name__)

# meiva:note
# How often a leftover is checked while root waits for it to end.
READY_POLL_S = 0.05


class LeftBehindMixin(StoppingMixin):
    # meiva:note
    # Stop a unit's recorded previous generation before a replacement may spawn.

    _run_dir: Path
    _config: SupervisorConfig
    _clock: Clock
    _process_control: ProcessControl

    # meiva:note
    # Stop this unit's recorded previous generation before spawning a new one.
    # Cleans up via standard pidfile and process group signalling.
    async def _clear_left_behind(self, runtime: UnitRuntime) -> None:
        unit_id = runtime.manifest.id
        pid = unit_ledger.read(self._run_dir, unit_id)
        if pid is None:
            return
        if not process_alive(pid):
            unit_ledger.clear(self._run_dir, unit_id)
            return
        self._emit_left_behind(runtime, pid)
        await self._stop_left_behind(runtime, pid)
        unit_ledger.clear(self._run_dir, unit_id)

    # meiva:note
    # TERM then KILL a recorded leftover, bounded by this unit's stop window.
    async def _stop_left_behind(self, runtime: UnitRuntime, pid: int) -> bool:
        if not self._signal_pid(pid, force=False):
            return not process_alive(pid)
        if await self._await_gone(pid, self._stop_window(runtime)):
            return True
        if not self._signal_pid(pid, force=True):
            return not process_alive(pid)
        return await self._await_gone(pid, self._config.stop_kill_reap_s)

    def _signal_pid(self, pid: int, *, force: bool) -> bool:
        if not process_alive(pid):
            return False
        signum = signal.SIGKILL if force else signal.SIGTERM
        try:
            group = self._process_control.getpgid(pid)
            if not process_alive(pid):
                return False
            if group == pid and group != self._own_group():
                self._process_control.killpg(group, signum)
            else:
                self._process_control.kill(pid, signum)
        except ProcessLookupError:
            return False
        return True

    # meiva:note
    # Poll a process until it is gone or time runs out.
    async def _await_gone(self, pid: int, timeout_s: float) -> bool:
        deadline = self._clock.monotonic() + timeout_s
        while True:
            if not process_alive(pid):
                return True
            if self._clock.monotonic() >= deadline:
                return False
            await self._clock.asleep(READY_POLL_S)

    def _emit_left_behind(self, runtime: UnitRuntime, pid: int) -> None:
        try:
            logger.warning(
                "unit {unit}: pid {pid} outlived its root — stopping it before spawning this root's own",
                event="root_unit_left_behind_reclaimed",
                unit=runtime.manifest.id,
                pid=pid,
            )
        except Exception:
            _log.exception("unit %s: left-behind event fan-out failed", runtime.manifest.id)
