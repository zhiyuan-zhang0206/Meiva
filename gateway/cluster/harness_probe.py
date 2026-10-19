from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from base.agents.birth.harness import LAUNCHABLE_HARNESSES
from base.api_contracts.agent_contract import Harness
from base.cluster import machines
from base.cluster.machine import MachineIdentity
from base.db import Database
from gateway.cluster.schemas import HarnessAvailability
from ops.rpc_schemas import HarnessProbeResult, HarnessProbeRow, OpKind

_HARNESS_CACHE_TTL_S = 30.0

DispatchOp = Callable[[Database, str, OpKind, dict[str, Any]], Awaitable[dict[str, Any]]]


# meiva:note
# The probe answers for the machine an agent would be born on, so it runs there —
# this module only asks and remembers. An answer is cached per machine for 30
# seconds because an install or a login changes at human speed, while the spawn
# form can be opened, closed and reopened many times in that window.
# meiva:link base/agents/docs/harness.meiva.okf.md
class HarnessProbeCache:
    def __init__(
        self,
        *,
        ttl_s: float = _HARNESS_CACHE_TTL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl_s = ttl_s
        self._monotonic = monotonic
        self._rows: dict[str, tuple[float, list[HarnessAvailability]]] = {}
        self._lock = threading.Lock()

    # meiva:note
    # A stale entry is dropped by the reader, so an expired answer is never
    # mistaken for a fresh one; the caller recomputes and stores the replacement.
    def get(self, machine: str) -> list[HarnessAvailability] | None:
        with self._lock:
            cached = self._rows.get(machine)
            if cached is None or self._monotonic() - cached[0] >= self._ttl_s:
                return None
            return cached[1]

    def store(self, machine: str, rows: list[HarnessAvailability]) -> None:
        with self._lock:
            self._rows[machine] = (self._monotonic(), rows)


# meiva:note
# `launchable` joins two separate facts and must not report either alone: a launcher
# for the harness exists in this build (LAUNCHABLE_HARNESSES, imported so the set has
# one owner) AND the machine can actually run it — installed and signed in, or the
# built-in runtime, which needs neither. Testing only the set would offer a choice
# the spawn then refuses.
# meiva:link base/agents/docs/harness.meiva.okf.md
def launchable(row: HarnessProbeRow) -> bool:
    if row.harness not in LAUNCHABLE_HARNESSES:
        return False
    return row.harness is Harness.MEIVA or row.status == "available"


def availability_rows(probed: HarnessProbeResult) -> list[HarnessAvailability]:
    return [
        HarnessAvailability(
            harness=row.harness,
            status=row.status,
            reason=row.reason,
            launchable=launchable(row),
        )
        for row in probed.harnesses
    ]


# meiva:note
# Only a machine that can run agents answers: a box without an agent-runner cannot
# birth one there, so its install state would be an answer to a question nobody can
# act on — it reports 503 like any other unrunnable target, and the form shows
# `unknown` rather than a `launchable` option that leads nowhere.
# meiva:link base/agents/docs/harness.meiva.okf.md
async def probe_harnesses(
    target: str, *, db: Database, local_machine: MachineIdentity, dispatch: DispatchOp
) -> HarnessProbeResult:
    if target == local_machine.name():
        role = local_machine.role()
    else:
        try:
            role = await asyncio.to_thread(machines.lookup_role, db, target)
        except machines.MachineNotRegistered:
            raise HTTPException(status_code=404, detail=f"unknown machine: {target!r}") from None
    if "agent-runner" not in role:
        raise HTTPException(
            status_code=503,
            detail=(
                f"machine {target!r} has no agent-runner ops server — "
                "it cannot run an agent, so it cannot report which harnesses it can run"
            ),
        )
    return HarnessProbeResult.model_validate(await dispatch(db, target, "harness_probe", {}))
