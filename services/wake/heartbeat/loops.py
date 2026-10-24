# meiva:link services/wake/heartbeat/docs/dispatch-round.meiva.okf.md
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import psycopg

from base.clock import Timers
from base.daemon.loop_health import LoopProgress
from base.daemon.round_loop import RoundFailureBudget
from services.wake.heartbeat.liveness import PASS_INTERVAL_S

_log = logging.getLogger(__name__)
_LIVENESS_BEAT_STEP_S = 30.0


class DispatchRound(Protocol):
    @property
    def interval_s(self) -> float: ...

    def run_round(self) -> None: ...


class LivenessRound(Protocol):
    async def run_round(self) -> None: ...


# meiva:link services/wake/heartbeat/docs/dispatch-round.meiva.okf.md
async def sleep_with_liveness(liveness: LoopProgress, total_s: float, *, timers: Timers) -> None:
    remaining = total_s
    while remaining > 0:
        liveness.beat()
        step = min(_LIVENESS_BEAT_STEP_S, remaining)
        await timers.asleep(step)
        remaining -= step


# meiva:link services/wake/heartbeat/docs/dispatch-round.meiva.okf.md
async def dispatch_loop(dispatch: DispatchRound, liveness: LoopProgress, *, timers: Timers) -> None:
    step = dispatch.interval_s
    failures = RoundFailureBudget("heartbeat poll")
    while True:
        try:
            await sleep_with_liveness(liveness, step, timers=timers)
            dispatch.run_round()
            failures.succeeded()
        except asyncio.CancelledError:
            raise
        except psycopg.ProgrammingError:
            _log.critical(
                "[heartbeat] schema / syntax error — code<->DB drift; retry will not self-heal, daemon exiting, restart after fix",
                exc_info=True,
            )
            raise
        except Exception as exc:
            _log.exception("[heartbeat] poll iteration failed")
            failures.failed(exc)


# meiva:link services/wake/heartbeat/docs/dispatch-round.meiva.okf.md
async def liveness_loop(
    observation: LivenessRound, liveness: LoopProgress, *, timers: Timers
) -> None:
    failures = RoundFailureBudget("heartbeat liveness")
    while True:
        try:
            await observation.run_round()
            failures.succeeded()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log.exception("[heartbeat] liveness loop iteration failed")
            failures.failed(exc)
        await sleep_with_liveness(liveness, PASS_INTERVAL_S, timers=timers)
