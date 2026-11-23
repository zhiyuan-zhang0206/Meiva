# meiva:note
# Reclaim the processes a previous root left behind before spawning new ones.
#
# Root stops recorded processes from previous generations before it spawns
# replacements, using standard pidfile and process group cleanup.
# meiva:link services/supervision/meiva_root/docs/meiva_root.md

from __future__ import annotations

import logging
from pathlib import Path

from base.clock import Clock
from base.log import logger
from base.native_process.ownership import OwnedProcess
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
        identity = unit_ledger.read(self._run_dir, unit_id)
        if identity is None:
            return
        if identity.live():
            self._emit_left_behind(runtime, identity.pid)
            await self._stop_left_behind(runtime, identity)
        unit_ledger.clear(self._run_dir, unit_id, identity=identity)

    async def _stop_left_behind(self, runtime: UnitRuntime, identity: OwnedProcess) -> None:
        self._signal_owned(identity, force=False)
        if not await self._await_gone(identity, self._stop_window(runtime)):
            self._signal_owned(identity, force=True)
            if not await self._await_gone(identity, self._config.stop_kill_reap_s):
                raise RuntimeError(
                    f"previous {runtime.manifest.id} process {identity.pid} survived root cleanup"
                )

    async def _await_gone(self, identity: OwnedProcess, timeout_s: float) -> bool:
        deadline = self._clock.monotonic() + timeout_s
        while identity.live():
            if self._clock.monotonic() >= deadline:
                return False
            await self._clock.asleep(READY_POLL_S)
        return True

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
