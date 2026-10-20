# meiva:note
# Reclaim the generations a previous root left behind before spawning new ones.
#
# Root owns every process it spawns, and a process it spawned outliving it is
# still its work: a crash, or the code-version gate's hard exit from a pool worker
# thread, leaves the tree running with nobody to reap it. The next root stops
# those recorded processes before it spawns replacements, so a unit is never
# reported as "state stopped / probe alive" — a leftover that keeps the port, the
# replacement that loses the bind, and a readiness answer that hides both.
#
# The spawn record (`unit_ledger`) is the whole identity: a recorded birth that
# no longer lives is dropped, a live recorded birth is stopped here, and a live
# process no record names is left alone — nothing distinguishes it from a
# neighbouring home's daemon, so the health monitor reports that case instead.
# meiva:link services/supervision/meiva_root/docs/meiva_root.meiva.okf.md

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from time import monotonic, time

from base.log import logger
from base.native_process.ownership import OwnedProcess
from base.native_process.root_control.ipc import UnitState
from services.supervision.meiva_root import unit_ledger
from services.supervision.meiva_root.stopping import StoppingMixin, SupervisorConfig
from services.supervision.meiva_root.unit_ledger import GenerationRecord
from services.supervision.meiva_root.unit_records import UnitRuntime

_log = logging.getLogger(__name__)

# meiva:note
# How often a non-child leftover is re-read while root waits for its recorded
# birth to end.
READY_POLL_S = 0.05


class LeftBehindMixin(StoppingMixin):
    # meiva:note
    # Stop a unit's recorded previous generation before a replacement may spawn.

    _run_dir: Path
    _config: SupervisorConfig

    # meiva:note
    # Stop this unit's recorded previous generation before spawning a new one;
    # returns whether the way is clear.
    #
    # A live recorded birth is a process this root's predecessor spawned for this
    # same unit and left running. It still holds the unit's port, socket or
    # instance lock, so a replacement loses the bind and exits while the leftover
    # keeps answering the readiness probe; the unit then reads "stopped" with a
    # live probe and nothing retries. A record whose process is gone is simply
    # dropped, and a live process with no record is never signalled here — the
    # health monitor reports that case instead (task #5474).
    async def _clear_left_behind(self, runtime: UnitRuntime) -> bool:
        unit_id = runtime.manifest.id
        record = unit_ledger.read(self._run_dir, unit_id)
        if record is None:
            return True
        if not record.identity.live():
            unit_ledger.clear(self._run_dir, unit_id)
            return True
        self._emit_left_behind(runtime, record)
        if await self._stop_left_behind(runtime, record.identity):
            unit_ledger.clear(self._run_dir, unit_id)
            return True
        runtime.state = UnitState.STOPPED
        runtime.last_error = (
            f"left-behind generation {record.generation} (pid {record.identity.pid}) "
            "did not stop; refusing to spawn a replacement onto its port"
        )
        try:
            logger.error(
                "unit {unit}: {detail}",
                event="root_unit_left_behind_stuck",
                unit=unit_id,
                generation=record.generation,
                pid=record.identity.pid,
                detail=runtime.last_error,
            )
        except Exception:
            _log.exception("unit %s: stuck-left-behind event fan-out failed", unit_id)
        return False

    # meiva:note
    # TERM then KILL a recorded leftover, bounded by this unit's stop window.
    async def _stop_left_behind(self, runtime: UnitRuntime, identity: OwnedProcess) -> bool:
        if not self._signal_owned(identity, force=False):
            return not identity.live()
        if await self._await_gone(identity, self._stop_window(runtime)):
            return True
        if not self._signal_owned(identity, force=True):
            return not identity.live()
        return await self._await_gone(identity, self._config.stop_kill_reap_s)

    # meiva:note
    # Poll a non-child process until its recorded birth is gone or time runs out.
    @staticmethod
    async def _await_gone(identity: OwnedProcess, timeout_s: float) -> bool:
        deadline = monotonic() + timeout_s
        while True:
            if not identity.live():
                return True
            if monotonic() >= deadline:
                return False
            await asyncio.sleep(READY_POLL_S)

    @staticmethod
    def _emit_left_behind(runtime: UnitRuntime, record: GenerationRecord) -> None:
        try:
            logger.warning(
                "unit {unit}: generation {generation} (pid {pid}) outlived its root "
                "for {left_behind_s:.0f}s — stopping it before spawning this root's own",
                event="root_unit_left_behind_reclaimed",
                unit=runtime.manifest.id,
                generation=record.generation,
                pid=record.identity.pid,
                left_behind_s=max(0.0, time() - record.started_at),
            )
        except Exception:
            _log.exception("unit %s: left-behind event fan-out failed", runtime.manifest.id)
